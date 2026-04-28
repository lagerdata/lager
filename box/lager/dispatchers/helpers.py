# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Shared helper functions for dispatcher modules.

These standalone functions provide common functionality used across
multiple dispatcher modules without requiring the full BaseDispatcher class.
They accept an error_class parameter for consistent error handling.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple, Type

from lager.cache import get_nets_cache


def find_saved_net(
    netname: str, error_class: Type[Exception]
) -> Dict[str, Any]:
    """
    Find a saved net by name.

    Uses the NetsCache for O(1) lookup.

    Args:
        netname: The name of the net to find.
        error_class: The exception class to raise on error.

    Returns:
        The net configuration dictionary.

    Raises:
        error_class: If the net is not found.
    """
    net = get_nets_cache().find_by_name(netname)
    if not net:
        raise error_class(
            f"Net '{netname}' not found. Create it with 'lager nets create'."
        )
    return net


def ensure_role(
    rec: Dict[str, Any],
    expected_role: str,
    error_class: Type[Exception],
) -> None:
    """
    Ensure that a net record has the expected role.

    Args:
        rec: The net configuration record.
        expected_role: The role the net should have.
        error_class: The exception class to raise on error.

    Raises:
        error_class: If the net has a different role.
    """
    actual_role = rec.get("role")
    if actual_role != expected_role:
        netname = rec.get("name", "<unknown>")
        raise error_class(
            f"Net '{netname}' is a '{actual_role}' net, not '{expected_role}'."
        )


def _find_mapping_for_net(
    rec: Dict[str, Any], netname: str
) -> Optional[Dict[str, Any]]:
    """
    Find the mapping entry for a specific net name.

    Args:
        rec: The net configuration record.
        netname: The net name to find mapping for.

    Returns:
        The mapping dictionary if found, None otherwise.
    """
    for m in rec.get("mappings") or []:
        if m.get("net") == netname:
            return m
    return None


def resolve_channel(
    rec: Dict[str, Any],
    netname: str,
    error_class: Type[Exception],
) -> int:
    """
    Resolve the channel/pin number for the net.

    Prefers mappings[].pin that matches this net; else falls back to
    top-level pin.

    Args:
        rec: The net configuration record.
        netname: The net name to resolve channel for.
        error_class: The exception class to raise on error.

    Returns:
        The channel number as an integer.

    Raises:
        error_class: If the channel cannot be resolved.
    """
    mapping = _find_mapping_for_net(rec, netname)
    pin = (mapping or {}).get("pin", rec.get("pin"))
    try:
        return int(pin)
    except (TypeError, ValueError):
        raise error_class(f"Invalid channel pin '{pin}' for net '{netname}'.")


def resolve_address(
    rec: Dict[str, Any],
    netname: str,
    error_class: Type[Exception],
) -> str:
    """
    Resolve the VISA/device address for the net.

    Prefers mappings[].device_override for this net if present;
    else uses rec['address'].

    Args:
        rec: The net configuration record.
        netname: The net name to resolve address for.
        error_class: The exception class to raise on error.

    Returns:
        The device address string.

    Raises:
        error_class: If no address is configured.
    """
    mapping = _find_mapping_for_net(rec, netname)
    addr = (mapping or {}).get("device_override") or rec.get("address")
    if not addr:
        raise error_class(f"Net '{rec.get('name')}' has no VISA address.")
    return addr


# Instrument-string -> hardware_service.py module name. Mirrors the regex
# switches in SupplyDispatcher._choose_driver and BatteryDispatcher._choose_driver
# (box/lager/power/{supply,battery}/dispatcher.py). Keep both in sync.
def _supply_module_for_instrument(inst: str) -> Optional[str]:
    inst = (inst or "").strip()
    if re.search(r"rigol[_\-\s]*dp8", inst, re.IGNORECASE):
        return "rigol_dp800"
    if re.search(r"keithley.*2281s", inst, re.IGNORECASE) or inst.lower() == "keithley_2281s":
        return "keithley"
    if re.search(r"keysight.*e36(2|3)\d\da", inst, re.IGNORECASE) or \
       inst.lower() in ("keysight_e36233a", "keysight_e36311a", "keysight_e36312a", "keysight_e36313a"):
        return "keysight_e36000"
    if inst in ("EA_PSB_10080_60", "EA_PSB_10060_60"):
        return "ea"
    return None


def _battery_module_for_instrument(inst: str) -> Optional[str]:
    inst = (inst or "").strip()
    if re.search(r"keithley.*2281s", inst, re.IGNORECASE) or inst.lower() == "keithley_2281s":
        return "keithley_battery"
    return None


def resolve_net_proxy(
    netname: str,
    role: str,
    error_class: Type[Exception],
) -> Tuple[str, Dict[str, Any], int]:
    """
    Resolve a net to (device_module_name, net_info, channel) suitable for
    constructing an `lager.nets.device.Device` HTTP proxy that POSTs to
    `hardware_service.py:/invoke`.

    Mirrors the address/channel/instrument logic of the per-role dispatchers'
    `_resolve_net_and_driver` / `_make_driver` but emits HTTP-friendly inputs
    instead of opening a VISA session in-process.

    Args:
        netname: The name of the net to resolve.
        role: The expected net role ("power-supply" or "battery").
        error_class: The exception class to raise on error.

    Returns:
        A tuple (device_module_name, net_info, channel) where:
          - device_module_name is the bare module name under
            lager.power.{supply,battery}.* that hardware_service.py will import
          - net_info carries {address, channel, instrument} — the keys each
            driver's create_device(net_info) consumes today
          - channel is the resolved channel/pin (also embedded in net_info)

    Raises:
        error_class: If the net is missing, has the wrong role, has no
            address/channel, or the instrument string isn't recognized for
            the given role.
    """
    rec = find_saved_net(netname, error_class)
    ensure_role(rec, role, error_class)
    address = resolve_address(rec, netname, error_class)
    channel = resolve_channel(rec, netname, error_class)
    instrument = rec.get("instrument") or ""

    if role == "power-supply":
        device_name = _supply_module_for_instrument(instrument)
    elif role == "battery":
        device_name = _battery_module_for_instrument(instrument)
    else:
        raise error_class(f"Unknown role '{role}' for net '{netname}'.")

    if device_name is None:
        raise error_class(
            f"Unsupported instrument '{instrument}' for {role} net '{netname}'."
        )

    net_info: Dict[str, Any] = {
        "address": address,
        "channel": channel,
        "instrument": instrument,
    }
    return device_name, net_info, channel
