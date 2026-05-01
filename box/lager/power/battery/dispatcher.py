# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Battery dispatcher using BaseDispatcher pattern.

This module provides the dispatcher layer for battery simulator operations,
routing commands to the appropriate backend driver based on net configuration.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Type

from lager.dispatchers.base import BaseDispatcher
from lager.exceptions import BatteryBackendError, LibraryMissingError, DeviceNotFoundError

from .battery_net import BatteryNet
from .keithley import KeithleyBattery


class BatteryDispatcher(BaseDispatcher):
    """
    Dispatcher for battery simulator operations.

    Routes operations to the appropriate battery simulator backend
    based on the instrument type configured in the net.
    """

    ROLE = "battery"
    ERROR_CLASS = BatteryBackendError
    _driver_cache: Dict[str, Any] = {}  # Class-level cache for this dispatcher type

    def _choose_driver(self, instrument_name: str) -> Type[BatteryNet]:
        """
        Return the driver class based on the instrument string.

        Args:
            instrument_name: The instrument identifier from the net configuration.

        Returns:
            The driver class to use for this instrument.

        Raises:
            BatteryBackendError: If the instrument is not supported.
        """
        inst = (instrument_name or "").strip()

        # Keithley 2281S Battery Simulator
        if re.search(r"keithley.*2281s", inst, re.IGNORECASE) or inst.lower() == "keithley_2281s":
            return KeithleyBattery

        raise self._make_error(f"Unsupported instrument for battery nets: '{instrument_name}'.")

    def _make_error(self, message: str) -> BatteryBackendError:
        """
        Create a BatteryBackendError with the given message.

        Args:
            message: The error message.

        Returns:
            A BatteryBackendError instance.
        """
        return BatteryBackendError(message)

    def _make_driver(
        self, rec: Dict[str, Any], netname: str, channel: int
    ) -> Any:
        """
        Construct or retrieve a cached driver instance.

        Overrides base class to handle Keithley's single-channel behavior.

        Args:
            rec: The net configuration record.
            netname: The net name.
            channel: The resolved channel number.

        Returns:
            A driver instance.

        Raises:
            BatteryBackendError: If driver construction fails.
        """
        instrument = rec.get("instrument") or ""
        address = self._resolve_address(rec, netname)

        # Check cache first
        cache_key = self._get_cache_key(instrument, address, netname, channel)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached

        # Create new driver
        Driver = self._choose_driver(instrument)

        try:
            if Driver is KeithleyBattery:
                # Single-channel battery simulator; backend accepts VISA address via 'address'
                driver = Driver(address=address, channel=1)
            else:
                # Future-proof fallback for multi-channel battery simulators
                driver = Driver(address=address, channel=channel)

            self._cache_driver(cache_key, driver)
            return driver

        except (LibraryMissingError, DeviceNotFoundError):
            # Bubble known exceptions for consistent exit codes
            raise
        except Exception as exc:
            # Wrap any other init error
            raise self._make_error(str(exc)) from exc


# Module-level dispatcher singleton
_dispatcher = BatteryDispatcher()


# --------- actions (called from battery.py / CLI impl scripts) ---------

def set_mode(netname: str, mode_type: str | None = None, **_):
    """Set or read battery simulation mode."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.mode(mode_type)


def set_to_battery_mode(netname: str, **_):
    """Set instrument to battery simulation mode."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.set_mode_battery()


def set(netname: str, **_):
    """Set instrument to battery simulation mode."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.set_mode_battery()


def set_soc(netname: str, value: float | None = None, **_):
    """Set or read battery state of charge."""
    if value is not None and not (0 <= value <= 100):
        raise BatteryBackendError("SOC must be between 0 and 100%")
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.soc(value=value)


def set_voc(netname: str, value: float | None = None, **_):
    """Set or read battery open circuit voltage."""
    if value is not None and value < 0:
        raise BatteryBackendError("Voltage cannot be negative")
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.voc(value=value)


def set_volt_full(netname: str, value: float | None = None, **_):
    """Set or read fully charged battery voltage."""
    if value is not None and value < 0:
        raise BatteryBackendError("Voltage cannot be negative")
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.voltage_full(value=value)


def set_volt_empty(netname: str, value: float | None = None, **_):
    """Set or read fully discharged battery voltage."""
    if value is not None and value < 0:
        raise BatteryBackendError("Voltage cannot be negative")
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.voltage_empty(value=value)


def set_capacity(netname: str, value: float | None = None, **_):
    """Set or read battery capacity."""
    if value is not None and value <= 0:
        raise BatteryBackendError("Capacity must be positive")
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.capacity(value=value)


def set_current_limit(netname: str, value: float | None = None, **_):
    """Set or read maximum charge/discharge current."""
    if value is not None and value < 0.001:
        raise BatteryBackendError("Current limit must be at least 1mA (0.001A)")
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.current_limit(value=value)


def set_ovp(netname: str, value: float | None = None, **_):
    """Set or read over-voltage protection threshold."""
    if value is not None and value < 0:
        raise BatteryBackendError("Voltage cannot be negative")
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.ovp(value=value)


def set_ocp(netname: str, value: float | None = None, **_):
    """Set or read over-current protection threshold."""
    if value is not None and value < 0.001:
        raise BatteryBackendError("OCP limit must be at least 1mA (0.001A)")
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.ocp(value=value)


def set_model(netname: str, partnumber: str | None = None, **_):
    """Set or read battery model."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.model(partnumber=partnumber)


def enable_battery(netname: str, **_):
    """Enable battery simulator output."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.enable()


def disable_battery(netname: str, **_):
    """Disable battery simulator output."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.disable()


def print_state(netname: str, **_):
    """Display comprehensive battery state."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    fields = None
    if hasattr(drv, "read_state_fields"):
        try:
            fields = drv.read_state_fields()
        except NotImplementedError:
            fields = None
    if fields is None:
        # Legacy: driver prints directly.
        drv.print_state()
        return
    from lager.cli_output import print_state as _print_state
    _print_state(
        netname,
        fields["fields"],
        command="battery.state",
        subject={"net": netname,
                 "instrument": fields.get("instrument"),
                 "channel": fields.get("channel")},
        title_severity=fields.get("severity", "ok"),
    )


def clear(netname: str, **_):
    """Clear protection trip conditions."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    # Clear both OVP and OCP as the CLI has a single clear command
    drv.clear_ovp()
    drv.clear_ocp()


def clear_ovp(netname: str, **_):
    """Clear OVP trip condition only."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.clear_ovp()


def clear_ocp(netname: str, **_):
    """Clear OCP trip condition only."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    drv.clear_ocp()


# Additional helper functions for direct measurement access
def terminal_voltage(netname: str, **_) -> float:
    """Get battery terminal voltage."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    return drv.terminal_voltage()


def current(netname: str, **_) -> float:
    """Get battery current."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    return drv.current()


def esr(netname: str, **_) -> float:
    """Get battery equivalent series resistance."""
    drv, _ = _dispatcher._resolve_net_and_driver(netname)
    return drv.esr()
