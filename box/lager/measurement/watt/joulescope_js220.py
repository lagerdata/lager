# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Joulescope JS220 watt meter implementation for power measurement.

Uses the joulescope Python package to interface with Joulescope JS220 hardware.
"""

from __future__ import annotations
import threading
from typing import Dict, Optional

import numpy as np

from .watt_net import WattMeterBase
from lager.exceptions import WattBackendError

try:
    import joulescope
except ModuleNotFoundError:
    joulescope = None


def _parse_serial(location) -> Optional[str]:
    """
    Parse serial number from location.
    Accept None, string serial number, or 'prefix:serial'.
    Returns None if no serial specified (use first available device).
    """
    if location is None:
        return None

    s = str(location).strip()
    if not s:
        return None

    # 'prefix:serial' → take last segment
    if ":" in s:
        parts = s.split(":")
        serial = parts[-1].strip()
        if serial:
            return serial

    # Return as-is if it looks like a serial number
    if s and s != "0":
        return s

    return None


class JoulescopeJS220(WattMeterBase):
    """
    Joulescope JS220 watt meter implementation with instance caching.

    This implementation uses the joulescope Python package to read power
    measurements from Joulescope JS220 hardware. Instances are cached per
    serial number to enable connection reuse.
    """

    # Class-level cache: one instance per serial number for connection reuse
    _instances: Dict[Optional[str], 'JoulescopeJS220'] = {}
    _instance_lock = threading.Lock()

    def __new__(cls, name: str, pin: int | str, location) -> 'JoulescopeJS220':
        """Factory method that returns cached instance for the same serial."""
        serial = _parse_serial(location)

        with cls._instance_lock:
            # Return existing instance if available (singleton per serial)
            if serial in cls._instances:
                instance = cls._instances[serial]
                # Update the name if it changed (same device, different net name)
                instance._name = name
                instance._pin = pin
                return instance

            # Create new instance
            instance = super().__new__(cls)
            instance._initializing = threading.Lock()  # Prevent concurrent initialization
            instance._initialized = False
            cls._instances[serial] = instance
            return instance

    def __init__(self, name: str, pin: int | str, location) -> None:
        """Initialize the Joulescope. Skips re-initialization for cached instances."""
        serial = _parse_serial(location)

        # Thread-safe initialization check
        with self._initializing:
            # Prevent re-initialization of cached instances
            if self._initialized:
                return

            super().__init__(name, pin)

            if joulescope is None:
                raise WattBackendError(
                    "Joulescope library not installed on box "
                    "(joulescope module import failed)"
                )

            self._serial = serial
            self._device = None

            # Open the Joulescope device
            try:
                if serial:
                    # Find specific device by serial number
                    devices = joulescope.scan()
                    matching = [d for d in devices if serial in str(d)]
                    if not matching:
                        raise WattBackendError(
                            f"Joulescope with serial '{serial}' not found. "
                            f"Available devices: {devices}"
                        )
                    self._device = joulescope.Device(matching[0])
                else:
                    # Use first available device
                    self._device = joulescope.scan_require_one(config='auto')

                self._device.open()
            except WattBackendError:
                raise
            except Exception as e:
                raise WattBackendError(
                    f"Failed to open Joulescope device: {e}"
                ) from e

            self._read_lock = threading.Lock()  # Thread-safe reads
            self._initialized = True

    def read_raw(self, duration: float) -> 'np.ndarray':
        """
        Return raw (N, 2) numpy array [current, voltage] for `duration` seconds.
        Package-internal method for reuse by energy analyzer without a second open().
        """
        with self._read_lock:
            if self._device is None:
                raise WattBackendError(
                    f"Joulescope '{self.name}' is not connected"
                )
            try:
                return self._device.read(contiguous_duration=duration)
            except Exception as e:
                raise WattBackendError(
                    f"Failed to read raw data from Joulescope '{self.name}': {e}"
                ) from e

    def read(self) -> float:
        """
        Read current power in watts (thread-safe).

        Returns:
            Power measurement in watts as a float
        """
        with self._read_lock:
            if self._device is None:
                raise WattBackendError(
                    f"Joulescope '{self.name}' is not connected"
                )

            try:
                # Read data for a short duration and compute mean values
                data = self._device.read(contiguous_duration=0.1)
                current, voltage = np.mean(data, axis=0, dtype=np.float64)
                power = current * voltage
                return float(power)
            except Exception as e:
                raise WattBackendError(
                    f"Failed to read from Joulescope '{self.name}': {e}"
                ) from e

    def read_current(self) -> float:
        """
        Read current in amps (thread-safe).

        Returns:
            Current measurement in amps as a float
        """
        with self._read_lock:
            if self._device is None:
                raise WattBackendError(
                    f"Joulescope '{self.name}' is not connected"
                )

            try:
                data = self._device.read(contiguous_duration=0.1)
                current, _ = np.mean(data, axis=0, dtype=np.float64)
                return float(current)
            except Exception as e:
                raise WattBackendError(
                    f"Failed to read current from Joulescope '{self.name}': {e}"
                ) from e

    def read_voltage(self) -> float:
        """
        Read voltage in volts (thread-safe).

        Returns:
            Voltage measurement in volts as a float
        """
        with self._read_lock:
            if self._device is None:
                raise WattBackendError(
                    f"Joulescope '{self.name}' is not connected"
                )

            try:
                data = self._device.read(contiguous_duration=0.1)
                _, voltage = np.mean(data, axis=0, dtype=np.float64)
                return float(voltage)
            except Exception as e:
                raise WattBackendError(
                    f"Failed to read voltage from Joulescope '{self.name}': {e}"
                ) from e

    def read_all(self) -> dict:
        """
        Read all measurements in a single operation (thread-safe).

        Returns:
            Dictionary with 'current' (amps), 'voltage' (volts), and 'power' (watts)
        """
        with self._read_lock:
            if self._device is None:
                raise WattBackendError(
                    f"Joulescope '{self.name}' is not connected"
                )

            try:
                data = self._device.read(contiguous_duration=0.1)
                current, voltage = np.mean(data, axis=0, dtype=np.float64)
                power = current * voltage
                return {
                    "current": float(current),
                    "voltage": float(voltage),
                    "power": float(power),
                }
            except Exception as e:
                raise WattBackendError(
                    f"Failed to read from Joulescope '{self.name}': {e}"
                ) from e

    def close(self) -> None:
        """Close the connection and release resources."""
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass  # Ignore errors during cleanup
            self._device = None

    def __del__(self) -> None:
        """Cleanup when instance is garbage collected."""
        self.close()

    @classmethod
    def clear_cache(cls) -> None:
        """
        Clear all cached instances and close all devices.
        Useful for testing or when you need to reset all connections.
        """
        with cls._instance_lock:
            for instance in cls._instances.values():
                instance.close()
            cls._instances.clear()
