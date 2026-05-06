# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Watt meter dispatcher that routes operations to the appropriate implementation.

This module provides a unified interface for watt meter operations,
automatically selecting the correct hardware backend based on net configuration.

Uses BaseDispatcher for shared infrastructure (net lookup, caching, etc.).
"""

from __future__ import annotations

from typing import Any, Dict, Type

from lager.dispatchers import BaseDispatcher
from lager.exceptions import WattBackendError

from .watt_net import WattMeterBase
from .yocto_watt import YoctoWatt


class WattMeterDispatcher(BaseDispatcher):
    """
    Dispatcher that routes watt meter operations to the appropriate implementation.

    Currently supports:
    - Yoctopuce Yocto-Watt (USB-based power measurement)
    - Joulescope JS220 (high-precision current/voltage/power measurement)

    This dispatcher uses BaseDispatcher infrastructure for:
    - Net lookup with O(1) caching
    - Role validation
    - Driver instance caching

    Usage:
        # Via module-level function
        from lager.measurement.watt import read
        power = read("my_watt_net")

        # Via class method
        watt = WattMeterDispatcher.from_net_name("my_watt_net")
        power = watt.read()

        # Via dispatcher directly
        dispatcher = WattMeterDispatcher()
        driver, channel = dispatcher._resolve_net_and_driver("my_watt_net")
        power = driver.read()
    """

    ROLE = "watt-meter"
    ERROR_CLASS = WattBackendError
    _driver_cache: Dict[str, Any] = {}  # Class-level cache for this dispatcher type

    def _choose_driver(self, instrument_name: str) -> Type[WattMeterBase]:
        """
        Return the driver class based on the instrument string.

        Supports:
        - Yoctopuce Yocto-Watt (default)
        - Joulescope JS220

        Args:
            instrument_name: The instrument identifier from the net configuration.

        Returns:
            The driver class to use for this instrument.
        """
        instrument_lower = instrument_name.lower()

        # Check for Joulescope instruments
        if 'joulescope' in instrument_lower or 'js220' in instrument_lower:
            from .joulescope_js220 import JoulescopeJS220
            return JoulescopeJS220

        # Check for Nordic PPK2 instruments
        if 'ppk2' in instrument_lower or 'ppk' in instrument_lower or 'nordic' in instrument_lower:
            from .ppk2_watt import PPK2Watt
            return PPK2Watt

        # Default to Yoctopuce for watt meters
        return YoctoWatt

    def _make_error(self, message: str) -> WattBackendError:
        """
        Create a WattBackendError.

        Args:
            message: The error message.

        Returns:
            A WattBackendError instance.
        """
        return WattBackendError(message)

    def _make_driver(
        self, rec: Dict[str, Any], netname: str, channel: int
    ) -> WattMeterBase:
        """
        Construct or retrieve a cached watt meter driver instance.

        Watt meters use a custom constructor signature that includes 'location'
        instead of the standard 'address' parameter.

        Args:
            rec: The net configuration record.
            netname: The net name.
            channel: The resolved channel number (pin).

        Returns:
            A watt meter driver instance.
        """
        instrument = rec.get("instrument") or "yoctopuce"
        location = rec.get("address") or f"yocto:{channel}"

        # Check cache first
        cache_key = self._get_cache_key(instrument, location, netname, channel)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached

        # Create new driver
        Driver = self._choose_driver(instrument)

        try:
            # Watt meter drivers use (name, pin, location) signature
            driver = Driver(name=netname, pin=channel, location=location)
            self._cache_driver(cache_key, driver)
            return driver
        except Exception as exc:
            raise self._make_error(str(exc)) from exc

    @classmethod
    def from_net_name(cls, netname: str) -> WattMeterBase:
        """
        Create a watt meter instance from a saved net name.

        This is a convenience method that creates a dispatcher and resolves
        the driver for the given net name.

        Args:
            netname: Name of the saved watt meter net

        Returns:
            WattMeterBase instance configured for the specified net

        Raises:
            WattBackendError: If net not found or has wrong type
        """
        dispatcher = cls()
        driver, _ = dispatcher._resolve_net_and_driver(netname)
        return driver


# Module-level singleton dispatcher
_dispatcher = WattMeterDispatcher()


def read(netname: str) -> float:
    """
    Read power from a watt meter net.

    Args:
        netname: Name of the saved watt meter net

    Returns:
        Power in watts

    Raises:
        WattBackendError: If net not found, wrong type, or read fails

    Example:
        from lager.measurement.watt import read
        power = read("my_watt_meter")
        print(f"Power: {power}W")
    """
    driver, _ = _dispatcher._resolve_net_and_driver(netname)
    return driver.read()


__all__ = [
    'WattMeterDispatcher',
    'read',
]
