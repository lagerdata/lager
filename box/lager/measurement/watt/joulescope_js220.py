# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Joulescope JS220 watt meter implementation for power measurement.

Uses the joulescope Python package to interface with Joulescope JS220 hardware.
"""

from __future__ import annotations
import threading
import time
from typing import Dict, Optional

import numpy as np

from .watt_net import WattMeterBase
from .accumulator import average_from_accumulators
from lager.exceptions import WattBackendError

try:
    import joulescope
except ModuleNotFoundError:
    joulescope = None


# A contiguous `device.read()` buffers every sample for the whole window in RAM
# and must fit the JS220's ~30s host stream buffer, so it is only used for short
# windows. Longer windows use the on-device statistics accumulators
# (`read_average`), which are gapless and hold no samples.
_MAX_CONTIGUOUS_SECONDS = 10.0
# Max seconds to wait for the first on-device statistics update after start().
_STATS_SETTLE_TIMEOUT = 3.0


def _parse_serial(location) -> Optional[str]:
    """
    Parse serial number from location.
    Accept None, string serial number, 'prefix:serial', or a VISA USB
    resource string ('USB0::0x16D0::0x10BA::004446::INSTR').
    Returns None if no serial specified (use first available device).
    """
    if location is None:
        return None

    s = str(location).strip()
    if not s:
        return None

    # VISA resource string. USB format: USB<n>::<vid>::<pid>::<serial>[::INSTR]
    # — the serial is the 4th field, so the plain ':'-split below would
    # misparse it (e.g. to 'INSTR'). Non-USB VISA resources carry no USB
    # serial, so scan for the first available device instead.
    if "::" in s:
        parts = [p.strip() for p in s.split("::")]
        if parts[0].upper().startswith("USB") and len(parts) >= 4:
            serial = parts[3]
            if serial and serial.upper() != "INSTR":
                return serial
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


def _device_serial(device) -> Optional[str]:
    """Best-effort serial identifier for a scanned device.

    The joulescope v1 Device exposes `serial_number` ('004446') and
    `device_path` ('u/js220/004446'); its repr is a bare object repr.
    """
    for attr in ("serial_number", "device_path"):
        try:
            value = getattr(device, attr, None)
        except Exception:
            value = None
        if value:
            return str(value)
    return None


def _matches_serial(device, serial: str) -> bool:
    """True if a scanned device matches `serial` (case-insensitive)."""
    ident = _device_serial(device)
    if ident is not None:
        return serial.lower() in ident.lower()
    # Last resort for API versions without the serial attributes, whose
    # str() may embed the serial (v1: 'JS220-004446').
    return serial.lower() in str(device).lower()


def _device_serials(devices) -> list:
    """Serial numbers (not object reprs) for the not-found error message."""
    return [_device_serial(d) or str(d) for d in devices]


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
            instance._serial = serial
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
                    # Find specific device by serial number. No fallback to
                    # scan_require_one on a miss: silently measuring the wrong
                    # device on a multi-Joulescope bench is worse than an error.
                    devices = joulescope.scan(config='auto')
                    matching = [d for d in devices if _matches_serial(d, serial)]
                    if not matching:
                        raise WattBackendError(
                            f"Joulescope with serial '{serial}' not found. "
                            f"Available devices: {_device_serials(devices)}"
                        )
                    # scan() returns ready Device objects. The v1 API has no
                    # top-level joulescope.Device, so re-wrapping broke every
                    # serial-specified open there.
                    self._device = matching[0]
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

    def read(self, duration: float = 0.1) -> float:
        """
        Read current power in watts (thread-safe).

        Args:
            duration: Averaging window in seconds. Longer windows average more
                samples for a lower-noise reading.

        Returns:
            Power measurement in watts as a float
        """
        if duration > _MAX_CONTIGUOUS_SECONDS:
            return self.read_average(duration)["power"]
        with self._read_lock:
            if self._device is None:
                raise WattBackendError(
                    f"Joulescope '{self.name}' is not connected"
                )

            try:
                # Read data for the requested duration and compute mean values
                data = self._device.read(contiguous_duration=duration)
                current, voltage = np.mean(data, axis=0, dtype=np.float64)
                power = current * voltage
                return float(power)
            except Exception as e:
                raise WattBackendError(
                    f"Failed to read from Joulescope '{self.name}': {e}"
                ) from e

    def read_current(self, duration: float = 0.1) -> float:
        """
        Read current in amps (thread-safe).

        Args:
            duration: Averaging window in seconds. Longer windows average more
                samples for a lower-noise reading.

        Returns:
            Current measurement in amps as a float
        """
        if duration > _MAX_CONTIGUOUS_SECONDS:
            return self.read_average(duration)["current"]
        with self._read_lock:
            if self._device is None:
                raise WattBackendError(
                    f"Joulescope '{self.name}' is not connected"
                )

            try:
                data = self._device.read(contiguous_duration=duration)
                current, _ = np.mean(data, axis=0, dtype=np.float64)
                return float(current)
            except Exception as e:
                raise WattBackendError(
                    f"Failed to read current from Joulescope '{self.name}': {e}"
                ) from e

    def read_voltage(self, duration: float = 0.1) -> float:
        """
        Read voltage in volts (thread-safe).

        Args:
            duration: Averaging window in seconds. Longer windows average more
                samples for a lower-noise reading.

        Returns:
            Voltage measurement in volts as a float
        """
        if duration > _MAX_CONTIGUOUS_SECONDS:
            return self.read_average(duration)["voltage"]
        with self._read_lock:
            if self._device is None:
                raise WattBackendError(
                    f"Joulescope '{self.name}' is not connected"
                )

            try:
                data = self._device.read(contiguous_duration=duration)
                _, voltage = np.mean(data, axis=0, dtype=np.float64)
                return float(voltage)
            except Exception as e:
                raise WattBackendError(
                    f"Failed to read voltage from Joulescope '{self.name}': {e}"
                ) from e

    def read_all(self, duration: float = 0.1) -> dict:
        """
        Read all measurements in a single operation (thread-safe).

        Args:
            duration: Averaging window in seconds. Longer windows average more
                samples for a lower-noise reading.

        Returns:
            Dictionary with 'current' (amps), 'voltage' (volts), and 'power' (watts)
        """
        if duration > _MAX_CONTIGUOUS_SECONDS:
            return self.read_average(duration)
        with self._read_lock:
            if self._device is None:
                raise WattBackendError(
                    f"Joulescope '{self.name}' is not connected"
                )

            try:
                data = self._device.read(contiguous_duration=duration)
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

    def read_average(self, duration: float) -> dict:
        """
        Gapless average current/voltage/power over a long window.

        Uses the JS220's on-device statistics accumulators (charge in coulombs,
        energy in joules) sampled at the window start and end:
        ``avg_current = Δcharge / Δt``, ``avg_power = Δenergy / Δt``, and
        ``avg_voltage = Δenergy / Δcharge`` (the charge-weighted mean voltage).

        Unlike a raw capture this holds no samples in memory and never drops
        data, so it scales to arbitrarily long windows and captures every
        transient.

        Args:
            duration: Averaging window in seconds.

        Returns:
            Dictionary with 'current' (amps), 'voltage' (volts), 'power' (watts)
        """
        with self._read_lock:
            if self._device is None:
                raise WattBackendError(
                    f"Joulescope '{self.name}' is not connected"
                )
            try:
                return self._accumulate_average(duration)
            except WattBackendError:
                raise
            except Exception as e:
                raise WattBackendError(
                    f"Failed to average from Joulescope '{self.name}': {e}"
                ) from e

    def _accumulate_average(self, duration: float) -> dict:
        """Integrate the on-device charge/energy accumulators over `duration`."""
        latest: Dict[str, dict] = {}

        def _on_statistics(data):
            latest["data"] = data

        self._device.statistics_callback_register(_on_statistics, "sensor")
        self._device.start()
        try:
            # Wait for the first on-device statistics update to arrive.
            waited = 0.0
            while "data" not in latest and waited < _STATS_SETTLE_TIMEOUT:
                time.sleep(0.05)
                waited += 0.05
            if "data" not in latest:
                raise WattBackendError(
                    f"No statistics from Joulescope '{self.name}'"
                )

            start = latest["data"]
            fs = float(start["time"]["sample_freq"]["value"])
            charge0 = float(start["accumulators"]["charge"]["value"])
            energy0 = float(start["accumulators"]["energy"]["value"])
            sample0 = float(start["time"]["samples"]["value"][1])

            # Let the accumulators integrate over the requested window.
            time.sleep(duration)

            end = latest["data"]
            charge1 = float(end["accumulators"]["charge"]["value"])
            energy1 = float(end["accumulators"]["energy"]["value"])
            sample1 = float(end["time"]["samples"]["value"][1])
            fallback_voltage = float(end["signals"]["voltage"]["avg"]["value"])

            try:
                return average_from_accumulators(
                    charge0, energy0, sample0,
                    charge1, energy1, sample1,
                    fs, fallback_voltage=fallback_voltage,
                )
            except ValueError as e:
                raise WattBackendError(
                    f"Statistics window too short on Joulescope '{self.name}': {e}"
                ) from e
        finally:
            try:
                self._device.stop()
            except Exception:
                pass
            try:
                self._device.statistics_callback_unregister(_on_statistics, "sensor")
            except Exception:
                try:
                    self._device.statistics_callback_unregister(_on_statistics)
                except Exception:
                    pass

    def _is_connection_alive(self) -> bool:
        """Health check used by dispatcher driver caches to drop closed devices."""
        return getattr(self, "_device", None) is not None

    def _close_device(self) -> None:
        """Close the device without touching the instance cache.

        Must not acquire _instance_lock: it is called by clear_cache() while
        the lock is held, and from __del__ (which the GC may run in a thread
        that already holds the lock).
        """
        device = getattr(self, "_device", None)
        if device is not None:
            try:
                device.close()
            except Exception:
                pass  # Ignore errors during cleanup
            self._device = None
        self._initialized = False

    def close(self) -> None:
        """Close the connection and release resources.

        Evicts this instance from the per-serial cache so the next
        construction for the same serial reopens the device. Without the
        eviction, a closed instance stays cached with _initialized=True and
        every later read fails with "is not connected" until the process
        restarts.
        """
        self._close_device()
        cls = type(self)
        with cls._instance_lock:
            # Identity check: don't evict a live replacement instance that
            # was created for this serial after we were closed.
            serial = getattr(self, "_serial", None)
            if cls._instances.get(serial) is self:
                del cls._instances[serial]

    def __del__(self) -> None:
        """Cleanup when instance is garbage collected."""
        # _close_device, not close(): __del__ must never take _instance_lock.
        self._close_device()

    @classmethod
    def clear_cache(cls) -> None:
        """
        Clear all cached instances and close all devices.
        Useful for testing or when you need to reset all connections.
        """
        with cls._instance_lock:
            for instance in cls._instances.values():
                instance._close_device()
            cls._instances.clear()
