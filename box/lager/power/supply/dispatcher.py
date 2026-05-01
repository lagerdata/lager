# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Supply dispatcher module for power supply control.

This module provides the dispatcher for routing power supply operations
to the appropriate hardware backend drivers.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Tuple, Type

from lager.dispatchers.base import BaseDispatcher
from lager.dispatchers import helpers
from lager.exceptions import SupplyBackendError, LibraryMissingError, DeviceNotFoundError
from lager.power.supply.rigol_dp800 import RigolDP800
from lager.power.supply.keithley import Keithley2281S
from lager.power.supply.ea import EA
from lager.power.supply.keysight_e36000 import KeysightE36000


class SupplyDispatcher(BaseDispatcher):
    """
    Dispatcher for power supply operations.

    Routes supply commands (voltage, current, enable, disable, etc.) to the
    appropriate hardware backend based on the instrument type configured for
    each net.
    """

    ROLE = "power-supply"
    ERROR_CLASS = SupplyBackendError
    _driver_cache: Dict[str, Any] = {}  # Class-level cache for driver instances

    def _choose_driver(self, instrument_name: str) -> Type[Any]:
        """
        Return the driver class based on the instrument string stored in the local net.
        """
        inst = (instrument_name or "").strip()

        # Rigol DP8xx
        if re.search(r"rigol[_\-\s]*dp8", inst, re.IGNORECASE):
            return RigolDP800

        # Keithley 2281S
        if re.search(r"keithley.*2281s", inst, re.IGNORECASE) or inst.lower() == "keithley_2281s":
            return Keithley2281S

        # Keysight E36xxx series (E36200 and E36300)
        # E36200: E36233A (dual-output, 30V/20A per channel)
        # E36300: E36311A, E36312A, E36313A (triple-output)
        if re.search(r"keysight.*e36(2|3)\d\da", inst, re.IGNORECASE) or \
           inst.lower() in ("keysight_e36233a", "keysight_e36311a", "keysight_e36312a", "keysight_e36313a"):
            return KeysightE36000

        # EA PSB (10080-60 / 10060-60)
        if inst in ("EA_PSB_10080_60", "EA_PSB_10060_60"):
            return EA

        raise self._make_error(f"Unsupported instrument for supply nets: '{instrument_name}'.")

    def _make_error(self, message: str) -> Exception:
        """Create a SupplyBackendError with the given message."""
        return self.ERROR_CLASS(message)

    def _make_driver(self, rec: Dict[str, Any], netname: str, channel: int) -> Any:
        """
        Construct the correct backend with the constructor signature it expects.

        This overrides the base class method to handle driver-specific
        constructor signatures.
        """
        instrument = rec.get("instrument") or ""
        address = self._resolve_address(rec, netname)

        # Check cache first
        cache_key = self._get_cache_key(instrument, address, netname, channel)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached

        Driver = self._choose_driver(instrument)

        try:
            if Driver is RigolDP800:
                # Multi-channel; pass (address, channel)
                driver = Driver(address=address, channel=channel)

            elif Driver is Keithley2281S:
                # Single-channel; backend accepts VISA address via 'instr'
                driver = Driver(instr=address, channel=1)

            elif Driver is EA:
                # Single-channel; pass VISA address via 'instr'
                driver = Driver(instr=address)

            elif Driver is KeysightE36000:
                # Multi-channel Keysight E36xxx; pass (address, channel)
                driver = Driver(address=address, channel=channel)

            else:
                # Future-proof fallback
                driver = Driver(address=address, channel=channel)

            self._cache_driver(cache_key, driver)
            return driver

        except (LibraryMissingError, DeviceNotFoundError):
            # Bubble known exceptions for consistent exit codes
            raise
        except Exception as exc:
            # Wrap any other init error
            raise self._make_error(str(exc)) from exc


# Module-level singleton dispatcher instance
_dispatcher = SupplyDispatcher()


def _resolve_net_and_driver(netname: str):
    """
    Module-level wrapper for dispatcher's _resolve_net_and_driver method.

    This function is used by http_handlers/supply.py for WebSocket monitoring.
    It provides backward compatibility by exposing the dispatcher method as a
    module-level function.

    Args:
        netname: The name of the net to resolve.

    Returns:
        A tuple of (driver_instance, channel_number).
    """
    return _dispatcher._resolve_net_and_driver(netname)


# --------- actions (called from supply.py) ---------

def voltage(netname: str, value: float | None = None, ocp: float | None = None, ovp: float | None = None, **_):
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.voltage(value=value, ocp=ocp, ovp=ovp)


def current(netname: str, value: float | None = None, ocp: float | None = None, ovp: float | None = None, **_):
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.current(value=value, ocp=ocp, ovp=ovp)


def enable(netname: str, **_):
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.enable()


def disable(netname: str, **_):
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.disable()


def state(netname: str, **_):
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    # Prefer the structured path: drivers that implement read_state_fields()
    # opt into the unified output (text-aligned + JSON envelope).
    fields = None
    if hasattr(drv, "read_state_fields"):
        try:
            fields = drv.read_state_fields()
        except NotImplementedError:
            fields = None
    if fields is None:
        # Legacy: driver prints directly.
        drv.state()
        return
    from lager.cli_output import print_state
    print_state(
        netname,
        fields["fields"],
        command="supply.state",
        subject={"net": netname,
                 "instrument": fields.get("instrument"),
                 "channel": fields.get("channel")},
        title_severity=fields.get("severity", "ok"),
    )


def set_mode(netname: str, **_):
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.set_mode()


def clear_ocp(netname: str, **_):
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.clear_ocp()


def clear_ovp(netname: str, **_):
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.clear_ovp()


def ocp(netname: str, value: float | None = None, **_):
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.ocp(value=value)


def ovp(netname: str, value: float | None = None, **_):
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.ovp(value=value)


def get_full_state(netname: str, **_):
    """Get full state including setpoints and limits"""
    drv, ch = _dispatcher._resolve_net_and_driver(netname)
    drv.get_full_state()
