# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Box-side backend for ``lager nets assign`` (custom serial devices).

Executes on the box via the same mechanism as ``net.py`` /
``query_instruments.py``. Commands (JSON on stdout; failures exit non-zero
with a message on stderr, which the CLI surfaces to the user):

    list             -> {"catalog": [...], "assignments": [...], "cables": [...]}
    assign  <json>   -> the stored assignment record (+ "address", "tty",
                        "roles", "channels", "deleted_nets")
    remove  <json>   -> {"removed": true|false, "deleted_nets": [...], ...}

``assign`` payload:  {"instrument", "serial" | "port_path", "baud"?}
``remove`` payload:  {"serial" | "port_path"}

A cable's vid/pid are captured from the live device, so ``assign`` requires
the cable to be plugged in. ``remove`` only consults the store. Saved nets
live and die with their assignment: removing (or replacing) an assignment
deletes the nets bound to its address — ``deleted_nets`` reports them.
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


def _delete_nets_for_address(address) -> list:
    """Delete saved nets bound to *address*; return their names.

    A net for a custom device is meaningless once its assignment is gone —
    the scanner no longer reports the instrument and ``nets add`` would
    refuse to recreate it — so assignment removal/replacement cascades to
    the nets. (The DP700 driver resolves ``serial://`` addresses from sysfs
    without consulting the store, so without this cascade a stale net would
    keep driving the instrument.) Guarded: returns [] when the nets module
    is unavailable.
    """
    if not address:
        return []
    try:
        from lager.nets.net import Net
    except Exception:
        return []
    try:
        nets = Net.get_local_nets()
    except Exception:
        return []
    deleted = [n.get("name") for n in nets if n.get("address") == address]
    if deleted:
        # save_local_nets also invalidates the box's nets cache, so the
        # warm /net/command path stops resolving these immediately.
        Net.save_local_nets([n for n in nets if n.get("address") != address])
    return deleted


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

    # One cable == one assignment. Replace any existing assignment for this
    # physical cable regardless of which identity form (serial vs port) it
    # was stored under, and cascade to its nets when they'd go stale:
    #   * same identity + same instrument (a --baud update): nets kept;
    #   * instrument changed: nets reference the wrong instrument — deleted;
    #   * identity form changed: the old address loses its assignment — its
    #     nets are deleted and the old record dropped (add() only upserts
    #     records with the identical identity key).
    new_key = (serial, port_path)
    deleted_nets: list = []
    for old in _custom_store.load():
        if old.get("vid") != cable["vid"] or old.get("pid") != cable["pid"]:
            continue
        same_cable = (
            (old.get("serial") and old.get("serial") == cable.get("serial"))
            or (old.get("port_path") and old.get("port_path") == cable.get("port_path"))
        )
        if not same_cable:
            continue
        old_key = (old.get("serial") or None, old.get("port_path") or None)
        if old_key == new_key and old.get("instrument") == instrument:
            continue  # baud-only update; the address and instrument stand
        deleted_nets.extend(_delete_nets_for_address(_safe_address(old)))
        if old_key != new_key:
            _custom_store.remove(old["vid"], old["pid"],
                                 serial=old.get("serial"),
                                 port_path=old.get("port_path"))

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
        "deleted_nets": deleted_nets,
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
    address = _safe_address(target)
    # Cascade: the assignment's nets go with it (see _delete_nets_for_address).
    deleted_nets = _delete_nets_for_address(address) if removed else []
    return {
        "removed": removed,
        "instrument": target.get("instrument"),
        "address": address,
        "deleted_nets": deleted_nets,
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
