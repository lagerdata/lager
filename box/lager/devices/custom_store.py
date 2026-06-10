# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Persistent store of user-assigned custom devices.

Maps a USB-serial cable identity to a catalog instrument (see
``lager.devices.catalog``) so the box can treat an otherwise-unclassifiable
cable as a known instrument. The scanner consults this store to surface the
assigned instrument, and the assign CLI writes to it.

Stored at ``/etc/lager/custom_devices.json`` (beside ``saved_nets.json``) as a
JSON array of records:

    {"instrument": "Rigol_DP711", "vid": "067b", "pid": "23a3",
     "serial": "00000006", "port_path": null}

Matching honors the project decision: **match by USB serial when the record
carries one, else by USB port path**. vid/pid must always match.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from lager.devices.catalog import canonical_name, serial_params
from lager.devices.serial_id import make_address, parse_address

STORE_PATH = os.environ.get(
    "LAGER_CUSTOM_DEVICES_PATH", "/etc/lager/custom_devices.json"
)


# --------------------------------- io ---------------------------------

def _load_raw() -> List[Dict[str, Any]]:
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        # A corrupt store should not take the box down; treat as empty.
        return []


def _atomic_write(records: List[Dict[str, Any]]) -> None:
    directory = os.path.dirname(STORE_PATH)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = f"{STORE_PATH}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
        os.replace(tmp, STORE_PATH)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass
        raise


def _norm(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip().lower()
    return v or None


def _identity_key(rec: Dict[str, Any]):
    """Tuple uniquely identifying a cable assignment (for de-dup)."""
    return (_norm(rec.get("vid")), _norm(rec.get("pid")),
            rec.get("serial") or None, rec.get("port_path") or None)


# ------------------------------- public api -------------------------------

def load() -> List[Dict[str, Any]]:
    """Return all stored assignments."""
    return _load_raw()


def add(instrument: str, vid: str, pid: str,
        serial: Optional[str] = None, port_path: Optional[str] = None,
        baud: Optional[int] = None) -> Dict[str, Any]:
    """Upsert an assignment of a cable identity to a catalog instrument.

    *baud* optionally overrides the catalog's default rate for this one
    assignment (the DP711's rate is set on its front panel, so it can differ
    per unit). Omitted entirely from the record when not given.

    Raises ValueError if *instrument* is unknown or no identity is provided.
    """
    canonical = canonical_name(instrument)
    if not canonical:
        raise ValueError(f"'{instrument}' is not a known custom device")
    if not serial and not port_path:
        raise ValueError("assignment requires a USB serial number or a port path")

    rec = {
        "instrument": canonical,
        "vid": _norm(vid),
        "pid": _norm(pid),
        "serial": serial or None,
        "port_path": port_path or None,
    }
    if baud is not None:
        rec["baud"] = int(baud)
    records = [r for r in _load_raw() if _identity_key(r) != _identity_key(rec)]
    records.append(rec)
    _atomic_write(records)
    return rec


def remove(vid: str, pid: str,
           serial: Optional[str] = None, port_path: Optional[str] = None) -> bool:
    """Remove a stored assignment. Returns True if one was removed."""
    target = _identity_key({"vid": vid, "pid": pid,
                            "serial": serial, "port_path": port_path})
    records = _load_raw()
    kept = [r for r in records if _identity_key(r) != target]
    if len(kept) == len(records):
        return False
    _atomic_write(kept)
    return True


def record_for(vid: str, pid: str,
               serial: Optional[str] = None, port_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the stored assignment record matching a cable identity, or None.

    Matches by serial when the stored record has one (and it equals the live
    serial); otherwise by port path. vid/pid must match.
    """
    vid_n, pid_n = _norm(vid), _norm(pid)
    for r in _load_raw():
        if _norm(r.get("vid")) != vid_n or _norm(r.get("pid")) != pid_n:
            continue
        if r.get("serial"):
            if serial and r["serial"] == serial:
                return r
        elif r.get("port_path"):
            if port_path and r["port_path"] == port_path:
                return r
    return None


def resolve(vid: str, pid: str,
            serial: Optional[str] = None, port_path: Optional[str] = None) -> Optional[str]:
    """Return the assigned instrument name for a live device, or None."""
    rec = record_for(vid, pid, serial=serial, port_path=port_path)
    return rec.get("instrument") if rec else None


def instrument_for_address(address: str) -> Optional[str]:
    """Map a durable ``serial://`` address to its assigned instrument, or None."""
    parts = parse_address(address)
    if not parts:
        return None
    return resolve(parts["vid"], parts["pid"],
                   serial=parts.get("serial"), port_path=parts.get("port_path"))


def serial_settings_for_address(address: str) -> Optional[Dict[str, Any]]:
    """Serial line settings for a ``serial://`` address, or None.

    The assigned instrument's catalog defaults (``catalog.serial_params``),
    overlaid with the assignment's stored overrides — currently just ``baud``,
    set with ``lager nets assign --baud`` when the instrument's front panel
    is not at the factory rate. None when the address is not a ``serial://``
    resource, has no assignment, or the instrument is not serial-transport.
    """
    parts = parse_address(address)
    if not parts:
        return None
    rec = record_for(parts["vid"], parts["pid"],
                     serial=parts.get("serial"), port_path=parts.get("port_path"))
    if not rec:
        return None
    settings = serial_params(rec.get("instrument"))
    if settings is None:
        return None
    if rec.get("baud"):
        settings["baud"] = int(rec["baud"])
    return settings


def address_for(rec: Dict[str, Any]) -> str:
    """Build the durable ``serial://`` address for a stored record."""
    return make_address(rec["vid"], rec["pid"],
                        serial=rec.get("serial"), port_path=rec.get("port_path"))
