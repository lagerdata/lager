# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Declarative catalog of manually-assignable ("custom") instruments.

Auto-detected USB-TMC instruments are described by the parallel ``SUPPORTED_USB``
/ ``CHANNEL_MAPS`` / ``INSTRUMENT_NET_MAP`` tables (in the USB scanner and the
nets CLI). Those tables are keyed on a USB VID:PID and assume the box can
identify the instrument on its own.

This catalog is the single source of truth for the *other* case: instruments
the box cannot identify by enumeration — e.g. a Rigol DP711, which is RS-232
only and reaches the box through a generic USB-serial adapter (Prolific
067b:23a3) that enumerates as the cable, not the instrument. A user manually
associates such a cable with a catalog entry; driver dispatch, net validation
and (later) scanner surfacing read from here.

Each entry is a plain dict so it can be serialized to the CLI side later
without importing driver code. Fields:

    display_name   Human label shown in the TUI assign flow.
    manufacturer   Vendor string (display only).
    roles          Net roles this instrument can serve (canonical hyphen names,
                   matching SUPPORTED_USB[...]["net_type"], e.g. "power-supply").
    channels       {role: [channel_id, ...]} — selectable channels per role.
    single_channel True if only one net may reference the instrument at a time.
    transport      "serial" (RS-232 over a USB-serial cable) or "usbtmc".
    serial         Serial line settings used to open the port (transport=serial).
                   Must match what is configured on the instrument's front panel.
    driver         "module:Class" of the backend driver (transport=serial uses a
                   driver that opens an ASRL/serial resource).
    idn_match      Regex the driver checks against ``*IDN?`` to confirm identity.
    limits         {role: {"voltage_max", "current_max"}} hardware ratings.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


DEVICE_CATALOG: Dict[str, Dict[str, Any]] = {
    "Rigol_DP711": {
        "display_name": "Rigol DP711",
        "manufacturer": "Rigol",
        "roles": ["power-supply"],
        "channels": {"power-supply": ["1"]},
        "single_channel": True,
        "transport": "serial",
        # DP711 RS-232: the baud rate is set on the instrument front panel
        # (System -> RS232). 9600 is the factory default; change this to match
        # the unit. Data/parity/stop are fixed by the DP700 series.
        "serial": {
            "baud": 9600,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
        },
        "driver": "lager.power.supply.rigol_dp700:RigolDP700",
        "idn_match": r"RIGOL\s+TECHNOLOGIES,\s*DP7",
        "limits": {
            # DP711: single channel, 30 V / 5 A (150 W).
            "power-supply": {"voltage_max": 30.0, "current_max": 5.0},
        },
    },
}


def get_device(name: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the catalog entry for ``name`` (case-insensitive), or None."""
    if not name:
        return None
    entry = DEVICE_CATALOG.get(name)
    if entry is not None:
        return entry
    lowered = name.strip().lower()
    for key, value in DEVICE_CATALOG.items():
        if key.lower() == lowered:
            return value
    return None


def is_custom_device(name: Optional[str]) -> bool:
    """True if ``name`` is a catalog (manually-assignable) instrument."""
    return get_device(name) is not None


def canonical_name(name: Optional[str]) -> Optional[str]:
    """Return the exact catalog key for ``name`` (case-insensitive), or None.

    Use this to normalize user-supplied instrument names to the canonical
    spelling (e.g. ``rigol_dp711`` -> ``Rigol_DP711``) before persisting.
    """
    if not name:
        return None
    if name in DEVICE_CATALOG:
        return name
    lowered = name.strip().lower()
    for key in DEVICE_CATALOG:
        if key.lower() == lowered:
            return key
    return None


def is_serial_transport(name: Optional[str]) -> bool:
    """True if ``name`` is a catalog instrument reached over a serial line."""
    entry = get_device(name)
    return bool(entry) and entry.get("transport") == "serial"


def serial_params(name: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the serial line settings for ``name``, or None if not serial."""
    entry = get_device(name)
    if not entry or entry.get("transport") != "serial":
        return None
    return dict(entry.get("serial") or {})


def roles_for(name: Optional[str]) -> List[str]:
    """Return the net roles ``name`` supports (empty list if unknown)."""
    entry = get_device(name)
    return list(entry.get("roles", [])) if entry else []


def channels_for(name: Optional[str], role: str) -> List[str]:
    """Return the selectable channels for ``name`` in ``role`` (may be empty)."""
    entry = get_device(name)
    if not entry:
        return []
    return list((entry.get("channels") or {}).get(role, []))


def limits_for(name: Optional[str], role: str) -> Optional[Dict[str, float]]:
    """Return {"voltage_max", "current_max"} for ``name``/``role`` if known."""
    entry = get_device(name)
    if not entry:
        return None
    limits = (entry.get("limits") or {}).get(role)
    return dict(limits) if limits else None
