# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Thermocouple dispatcher that routes operations to the appropriate implementation.

This module provides a unified interface for thermocouple temperature measurement,
automatically selecting the correct hardware backend based on net configuration.

Uses BaseDispatcher for shared infrastructure (net lookup, caching, etc.).
"""

from __future__ import annotations

from typing import Any, Dict, Type

from lager.dispatchers import BaseDispatcher
from lager.exceptions import ThermocoupleBackendError

from .thermocouple_net import ThermocoupleBase
from .phidget import PhidgetThermocouple


class ThermocoupleDispatcher(BaseDispatcher):
    """
    Dispatcher that routes thermocouple operations to the appropriate implementation.

    Currently supports:
    - Phidget thermocouple sensors

    This dispatcher uses BaseDispatcher infrastructure for:
    - Net lookup with O(1) caching
    - Role validation
    - Driver instance caching

    Usage:
        # Via module-level function
        from lager.measurement.thermocouple import read
        temp = read("my_thermocouple_net")

        # Via dispatcher directly
        dispatcher = ThermocoupleDispatcher()
        driver, channel = dispatcher._resolve_net_and_driver("my_thermocouple_net")
        temp = driver.read()
    """

    ROLE = "thermocouple"
    ERROR_CLASS = ThermocoupleBackendError
    _driver_cache: Dict[str, Any] = {}  # Class-level cache for this dispatcher type

    def _choose_driver(self, instrument_name: str) -> Type[ThermocoupleBase]:
        """
        Return the driver class based on the instrument string.

        Currently all thermocouple nets use Phidget, but this can be extended
        to support other thermocouple hardware in the future.

        Args:
            instrument_name: The instrument identifier from the net configuration.

        Returns:
            The driver class to use for this instrument.
        """
        # Default to Phidget for thermocouples
        # Add more cases here as other thermocouple hardware is supported
        return PhidgetThermocouple

    def _make_error(self, message: str) -> ThermocoupleBackendError:
        """
        Create a ThermocoupleBackendError.

        Args:
            message: The error message.

        Returns:
            A ThermocoupleBackendError instance.
        """
        return ThermocoupleBackendError(message)

    def _make_driver(
        self, rec: Dict[str, Any], netname: str, channel: int
    ) -> ThermocoupleBase:
        """
        Construct or retrieve a cached thermocouple driver instance.

        Thermocouples use a custom constructor signature that includes 'location'
        instead of the standard 'address' parameter.

        Args:
            rec: The net configuration record.
            netname: The net name.
            channel: The resolved channel number (pin).

        Returns:
            A thermocouple driver instance.
        """
        instrument = rec.get("instrument") or "phidget"
        location = rec.get("address") or f"phidget:{channel}"

        # Check cache first
        cache_key = self._get_cache_key(instrument, location, netname, channel)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached

        # Create new driver
        Driver = self._choose_driver(instrument)

        try:
            # Thermocouple drivers use (name, pin, location) signature
            driver = Driver(name=netname, pin=channel, location=location)
            self._cache_driver(cache_key, driver)
            return driver
        except Exception as exc:
            raise self._make_error(str(exc)) from exc


# Module-level singleton dispatcher
_dispatcher = ThermocoupleDispatcher()


def read(netname: str) -> float:
    """
    Read temperature from a thermocouple net.

    Args:
        netname: Name of the saved thermocouple net

    Returns:
        Temperature in degrees Celsius

    Raises:
        ThermocoupleBackendError: If net not found, wrong type, or read fails

    Example:
        from lager.measurement.thermocouple import read
        temp = read("my_thermocouple")
        print(f"Temperature: {temp}C")
    """
    driver, _ = _dispatcher._resolve_net_and_driver(netname)
    return driver.read()


__all__ = [
    'ThermocoupleDispatcher',
    'read',
]
