# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
UART dispatcher - loads net configs and creates UART bridge driver instances.

Uses shared helpers from lager.dispatchers.helpers for common patterns.
"""
from __future__ import annotations

import sys
from typing import Any

from lager.dispatchers import helpers
from lager.exceptions import UARTBackendError

# Re-export for backward compatibility with modules that import from here
__all__ = ['UARTBackendError', 'monitor', 'monitor_interactive', '_resolve_net_and_driver']

# Role constant for UART nets
ROLE = "uart"


# --------- UART-specific helpers ---------

def _resolve_bridge_serial(rec: dict[str, Any]) -> str:
    """Get the UART bridge USB serial number from the pin field."""
    bridge_serial = rec.get("pin")
    if isinstance(bridge_serial, str) and bridge_serial.startswith("/dev/"):
        # A direct device path was stored instead of a USB serial.
        return ""
    if not bridge_serial:
        raise UARTBackendError(f"Net '{rec.get('name')}' has no USB serial number (pin field).")
    return bridge_serial


def _resolve_device_path(rec: dict[str, Any]) -> str | None:
    """
    Get a direct device path if provided.

    Supported sources:
    - device_path field (explicitly stored)
    - pin field when it contains a /dev/* path
    """
    if rec.get("device_path"):
        return rec["device_path"]
    pin = rec.get("pin")
    if isinstance(pin, str) and pin.startswith("/dev/"):
        return pin
    return None


def _resolve_port(rec: dict[str, Any]) -> str:
    """Get the port number from the channel field."""
    port = rec.get("channel", "0")
    return str(port)


def _make_driver(rec: dict[str, Any], overrides: dict[str, Any]):
    """
    Construct a UART bridge driver with the net configuration and overrides.
    """
    from .uart_bridge import UARTBridge

    bridge_serial = _resolve_bridge_serial(rec)
    device_path = _resolve_device_path(rec)
    port = _resolve_port(rec)
    params = rec.get("params", {})

    # Merge params with overrides (overrides take precedence)
    final_params = {**params, **overrides}

    try:
        return UARTBridge(bridge_serial, port, device_path=device_path,
                          usb_identity=rec.get("usb_identity"), **final_params)
    except Exception as exc:
        raise UARTBackendError(f"Failed to create UART driver: {exc}") from exc


def _resolve_net_and_driver(netname: str, overrides: dict[str, Any] = None):
    """Load net config and create driver instance.

    Uses shared helpers for net lookup and role validation.
    """
    if overrides is None:
        overrides = {}

    # Use shared helpers for common patterns
    rec = helpers.find_saved_net(netname, UARTBackendError)
    helpers.ensure_role(rec, ROLE, UARTBackendError)

    drv = _make_driver(rec, overrides)
    return drv


# --------- actions (called from uart_monitor.py) ---------

def monitor(netname: str, overrides: dict[str, Any] = None, **_):
    """Monitor UART output (read-only mode)."""
    drv = _resolve_net_and_driver(netname, overrides)
    try:
        drv.monitor()
    except KeyboardInterrupt:
        print("\nDisconnected", file=sys.stderr)


def monitor_interactive(netname: str, overrides: dict[str, Any] = None, **_):
    """Monitor UART with interactive input capability."""
    drv = _resolve_net_and_driver(netname, overrides)
    try:
        drv.monitor_interactive()
    except KeyboardInterrupt:
        print("\nDisconnected", file=sys.stderr)
