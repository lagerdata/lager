# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Nordic PPK2 (Power Profiler Kit II) watt meter implementation.

Uses the ppk2-api package to interface with Nordic PPK2 hardware via USB CDC serial.
The PPK2 operates in source mode: it supplies a configurable voltage (0.8–5V) and
measures current. Power is computed as voltage × current.
"""

from __future__ import annotations
import threading
import time
from typing import Dict, Optional, Tuple

import numpy as np

from .watt_net import WattMeterBase
from lager.exceptions import WattBackendError

try:
    from ppk2_api.ppk2_api import PPK2_API
except ModuleNotFoundError:
    PPK2_API = None

DEFAULT_VOLTAGE_MV = 3300


def _parse_location(location) -> Tuple[Optional[str], int]:
    """
    Parse serial number and voltage from location string.

    Formats:
        None                    → (None, 3300)
        'ppk2:SERIAL:VOLTAGE'  → (SERIAL, VOLTAGE)
        'ppk2:SERIAL'          → (SERIAL, 3300)
        'SERIAL'               → (SERIAL, 3300)

    Returns:
        (serial_or_None, voltage_mv)
    """
    if location is None:
        return (None, DEFAULT_VOLTAGE_MV)

    s = str(location).strip()
    if not s:
        return (None, DEFAULT_VOLTAGE_MV)

    parts = s.split(":")
    # Strip 'ppk2' prefix if present
    if parts[0].lower() == "ppk2":
        parts = parts[1:]

    if not parts or not parts[0].strip():
        return (None, DEFAULT_VOLTAGE_MV)

    serial = parts[0].strip()
    if serial == "0":
        serial = None

    voltage_mv = DEFAULT_VOLTAGE_MV
    if len(parts) >= 2 and parts[1].strip():
        try:
            voltage_mv = int(parts[1].strip())
        except ValueError:
            pass

    return (serial, voltage_mv)


class PPK2Watt(WattMeterBase):
    """
    Nordic PPK2 watt meter implementation with instance caching.

    Operates in source mode: supplies a configurable voltage and measures current.
    Instances are cached per serial number to enable connection reuse.
    """

    _instances: Dict[Optional[str], 'PPK2Watt'] = {}
    _instance_lock = threading.Lock()

    def __new__(cls, name: str, pin: int | str, location) -> 'PPK2Watt':
        """Factory method that returns cached instance for the same serial."""
        serial, _ = _parse_location(location)

        with cls._instance_lock:
            if serial in cls._instances:
                instance = cls._instances[serial]
                instance._name = name
                instance._pin = pin
                return instance

            instance = super().__new__(cls)
            instance._initializing = threading.Lock()
            instance._initialized = False
            cls._instances[serial] = instance
            return instance

    def __init__(self, name: str, pin: int | str, location) -> None:
        """Initialize the PPK2. Skips re-initialization for cached instances."""
        serial, voltage_mv = _parse_location(location)

        with self._initializing:
            if self._initialized:
                return

            super().__init__(name, pin)

            if PPK2_API is None:
                raise WattBackendError(
                    "ppk2-api library not installed on box "
                    "(ppk2_api module import failed)"
                )

            self._serial = serial
            self._voltage_mv = voltage_mv
            self._device = None

            try:
                devices = PPK2_API.list_devices()
                if not devices:
                    raise WattBackendError("No PPK2 devices found")

                # ppk2_api.list_devices() returns a flat list of port
                # path strings, e.g. ['/dev/ttyACM0', '/dev/ttyACM1'].
                # The PPK2 exposes two CDC ACM interfaces; only one is
                # the data port.  Try each until get_modifiers() succeeds.
                last_err = None
                for port in devices:
                    try:
                        self._device = PPK2_API(port)
                        self._device.get_modifiers()
                        break  # success
                    except Exception as e:
                        self._device = None
                        last_err = e
                else:
                    raise WattBackendError(
                        f"No usable PPK2 port among {devices}: {last_err}"
                    )

                self._device.use_source_meter()
                self._device.set_source_voltage(self._voltage_mv)
            except WattBackendError:
                raise
            except Exception as e:
                raise WattBackendError(
                    f"Failed to open PPK2 device: {e}"
                ) from e

            self._read_lock = threading.Lock()
            self._initialized = True

    def read_raw(self, duration: float) -> Tuple[np.ndarray, float]:
        """
        Collect current samples for `duration` seconds.

        Returns:
            Tuple of (current_samples_amps, voltage_volts).
            current_samples_amps is a 1-D numpy array of current in amps.
            voltage_volts is the configured source voltage.
        """
        with self._read_lock:
            if self._device is None:
                raise WattBackendError(
                    f"PPK2 '{self.name}' is not connected"
                )

            try:
                self._device.start_measuring()
                time.sleep(duration)
                self._device.stop_measuring()

                # Collect all available data
                read_data = self._device.get_data()
                if read_data is None or len(read_data) == 0:
                    raise WattBackendError(
                        f"PPK2 '{self.name}' returned no data"
                    )

                samples = self._device.get_samples(read_data)
                if samples is None or len(samples) == 0:
                    raise WattBackendError(
                        f"PPK2 '{self.name}' returned no samples"
                    )

                # ppk2-api returns current in microamps; convert to amps
                current_amps = np.array(samples, dtype=np.float64) * 1e-6
                voltage_v = self._voltage_mv / 1000.0
                return (current_amps, voltage_v)
            except WattBackendError:
                raise
            except Exception as e:
                raise WattBackendError(
                    f"Failed to read raw data from PPK2 '{self.name}': {e}"
                ) from e

    def read(self) -> float:
        """
        Read current power in watts (thread-safe).

        Returns:
            Power measurement in watts as a float
        """
        current_amps, voltage_v = self.read_raw(0.1)
        mean_current = float(np.mean(current_amps))
        return mean_current * voltage_v

    def read_current(self) -> float:
        """
        Read current in amps (thread-safe).

        Returns:
            Current measurement in amps as a float
        """
        current_amps, _ = self.read_raw(0.1)
        return float(np.mean(current_amps))

    def read_voltage(self) -> float:
        """
        Read voltage in volts (thread-safe).

        Returns the configured source voltage (PPK2 does not independently measure voltage).

        Returns:
            Voltage in volts as a float
        """
        return self._voltage_mv / 1000.0

    def read_all(self) -> dict:
        """
        Read all measurements in a single operation (thread-safe).

        Returns:
            Dictionary with 'current' (amps), 'voltage' (volts), and 'power' (watts)
        """
        current_amps, voltage_v = self.read_raw(0.1)
        mean_current = float(np.mean(current_amps))
        power = mean_current * voltage_v
        return {
            "current": mean_current,
            "voltage": voltage_v,
            "power": power,
        }

    def close(self) -> None:
        """Close the connection and release resources."""
        if self._device is not None:
            try:
                self._device.stop_measuring()
            except Exception:
                pass
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
