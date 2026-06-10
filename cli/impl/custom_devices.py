# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Box-side backend for ``lager nets assign`` (custom serial devices).

Executes on the box via the same mechanism as ``net.py`` /
``query_instruments.py``. Commands (JSON on stdout; failures exit non-zero
with a message on stderr, which the CLI surfaces to the user):

    list             -> {"catalog": [...], "assignments": [...], "cables": [...]}
    assign  <json>   -> the stored assignment record (+ "address", "tty", ...)
    remove  <json>   -> {"removed": true|false, ...}

``assign`` payload:  {"instrument", "serial" | "port_path", "baud"?}
``remove`` payload:  {"serial" | "port_path"}

A cable's vid/pid are captured from the live device, so ``assign`` requires
the cable to be plugged in. ``remove`` only consults the store.
"""

import json
import sys

try:
    # Custom-device framework; absent on box images that predate it.
    from lager.devices import catalog as _catalog
    from lager.devices import custom_store as _custom_store
    from lager.devices import serial_id as _serial_id
except Exception:
    _catalog = _custom_store = _serial_id = None


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def _require_framework() -> None:
    if _custom_store is None:
        _fail(
            "This box's software predates custom serial devices. "
            "Update the box (lager update) and retry."
        )


def _safe_address(rec: dict):
    try:
        return _custom_store.address_for(rec)
    except Exception:
        return None


def _catalog_entries() -> list:
    """Serializable summaries of the assignable instruments."""
    entries = []
    for name in sorted(_catalog.DEVICE_CATALOG):
        entry = _catalog.DEVICE_CATALOG[name]
        entries.append({
            "name": name,
            "display_name": entry.get("display_name", name),
            "manufacturer": entry.get("manufacturer"),
            "roles": list(entry.get("roles", [])),
            "channels": {r: list(c) for r, c in (entry.get("channels") or {}).items()},
            "transport": entry.get("transport"),
            "default_baud": (entry.get("serial") or {}).get("baud"),
        })
    return entries


def _cmd_list() -> dict:
    assignments = []
    for rec in _custom_store.load():
        tty = _serial_id.resolve_tty(
            rec.get("vid"), rec.get("pid"),
            serial=rec.get("serial"), port_path=rec.get("port_path"),
        )
        assignments.append({**rec, "address": _safe_address(rec), "tty": tty})

    # Candidate cables: live USB-serial cables not already assigned.
    assigned_ttys = {a["tty"] for a in assignments if a.get("tty")}
    cables = [c for c in _serial_id.list_cables() if c["tty"] not in assigned_ttys]

    return {
        "catalog": _catalog_entries(),
        "assignments": assignments,
        "cables": cables,
    }


def _identity_from(payload: dict):
    """Extract the exactly-one-of serial/port_path identity from a payload."""
    serial = payload.get("serial") or None
    port_path = payload.get("port_path") or None
    if bool(serial) == bool(port_path):
        _fail("Provide exactly one of a USB serial number or a USB port path.")
    return serial, port_path


def _cmd_assign(payload: dict) -> dict:
    instrument = _catalog.canonical_name(payload.get("instrument"))
    if not instrument:
        known = ", ".join(sorted(_catalog.DEVICE_CATALOG))
        _fail(f"Unknown device '{payload.get('instrument')}'. Assignable devices: {known}")
    serial, port_path = _identity_from(payload)

    matches = [
        c for c in _serial_id.list_cables()
        if (serial and c.get("serial") == serial)
        or (port_path and c.get("port_path") == port_path)
    ]
    if not matches:
        what = f"serial number {serial}" if serial else f"port path {port_path}"
        _fail(
            f"No USB-serial cable with {what} is currently connected. "
            f"The cable must be plugged in to assign it (its USB identity is "
            f"captured from the live device)."
        )
    if len(matches) > 1:
        ttys = ", ".join(c["tty"] for c in matches)
        _fail(
            f"Multiple connected cables match ({ttys}). Unplug the extras, "
            f"or assign by port path instead."
        )

    cable = matches[0]
    rec = _custom_store.add(
        instrument, cable["vid"], cable["pid"],
        serial=serial, port_path=port_path,
        baud=payload.get("baud"),
    )
    entry = _catalog.get_device(instrument) or {}
    return {
        **rec,
        "address": _custom_store.address_for(rec),
        "tty": cable["tty"],
        # Catalog facts the CLI needs for messaging / --as-net.
        "roles": list(entry.get("roles", [])),
        "channels": {r: list(c) for r, c in (entry.get("channels") or {}).items()},
    }


def _cmd_remove(payload: dict) -> dict:
    serial, port_path = _identity_from(payload)
    target = None
    for rec in _custom_store.load():
        if serial and rec.get("serial") == serial:
            target = rec
        elif port_path and rec.get("port_path") == port_path:
            target = rec
    if target is None:
        return {"removed": False}
    removed = _custom_store.remove(
        target["vid"], target["pid"],
        serial=target.get("serial"), port_path=target.get("port_path"),
    )
    return {
        "removed": removed,
        "instrument": target.get("instrument"),
        "address": _safe_address(target),
    }


def main(argv=None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        _fail("usage: custom_devices.py list | assign <json> | remove <json>")
    _require_framework()

    cmd = argv[0]
    if cmd == "list":
        result = _cmd_list()
    elif cmd in ("assign", "remove"):
        if len(argv) < 2:
            _fail(f"usage: custom_devices.py {cmd} <json>")
        try:
            payload = json.loads(argv[1])
        except json.JSONDecodeError as exc:
            _fail(f"Invalid JSON payload: {exc}")
        result = _cmd_assign(payload) if cmd == "assign" else _cmd_remove(payload)
    else:
        _fail(f"Unknown command '{cmd}'. Expected: list, assign, remove.")

    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
