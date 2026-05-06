# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Phidget thermocouple implementation for temperature measurement.

Uses the Phidget22 API to interface with Phidget thermocouple hardware.
"""

from __future__ import annotations

import threading
import fcntl
import os
import time
from typing import Dict

from .thermocouple_net import ThermocoupleBase
from lager.exceptions import ThermocoupleBackendError

try:
    from Phidget22.Devices.TemperatureSensor import TemperatureSensor
except ModuleNotFoundError:
    TemperatureSensor = None  # so import won't crash on non-box machines

# Cross-process lock directory
LOCK_DIR = "/tmp/lager_phidget_locks"

def _parse_channel(location) -> int:
    """
    Accept None, int, numeric string, or 'prefix:NN'.
    Defaults to channel 0 if nothing parseable is provided.
    """
    if location is None:
        return 0

    if isinstance(location, int):
        return location

    s = str(location).strip()
    if not s:
        return 0

    # 'prefix:NN' → take last segment
    if ":" in s:
        parts = s.split(":")
        try:
            return int(parts[-1].strip())
        except Exception:
            pass

    # bare integer string
    try:
        return int(s, 10)
    except Exception:
        pass

    # last resort: pull trailing digits
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits:
        try:
            return int(digits, 10)
        except Exception:
            pass

    return 0

class _PhidgetChannelLock:
    """
    File-based lock for cross-process synchronization of Phidget channel access.
    Prevents multiple processes from simultaneously accessing the same hardware channel.
    """
    def __init__(self, channel: int, timeout: float = 10.0):
        self.channel = channel
        self.timeout = timeout
        self.lock_file = None
        self.lock_path = None

    def __enter__(self):
        # Ensure lock directory exists
        os.makedirs(LOCK_DIR, exist_ok=True)

        # Create channel-specific lock file
        self.lock_path = os.path.join(LOCK_DIR, f"phidget_channel_{self.channel}.lock")
        self.lock_file = open(self.lock_path, 'w')

        # Try to acquire lock with timeout and retry
        start_time = time.time()
        retry_delay = 0.1  # Start with 100ms
        max_retry_delay = 1.0  # Max 1 second between retries

        while True:
            try:
                # Non-blocking lock attempt
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self  # Lock acquired successfully
            except BlockingIOError:
                # Lock held by another process
                elapsed = time.time() - start_time
                if elapsed >= self.timeout:
                    self.lock_file.close()
                    raise TimeoutError(
                        f"Failed to acquire lock for Phidget channel {self.channel} "
                        f"after {self.timeout}s (another process is using this channel)"
                    )

                # Exponential backoff with jitter
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, max_retry_delay)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.lock_file:
            try:
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
                self.lock_file.close()
            except Exception:
                pass  # Ignore cleanup errors
        return False

class PhidgetThermocouple(ThermocoupleBase):
    """Phidget Thermocouple implementation (returns degrees C) with instance caching."""

    # Class-level cache: one instance per channel for connection reuse
    _instances: Dict[int, 'PhidgetThermocouple'] = {}
    _instance_lock = threading.Lock()

    def __new__(cls, name: str, pin: int | str, location) -> 'PhidgetThermocouple':
        """Factory method that returns cached instance for the same channel."""
        channel = _parse_channel(location)

        with cls._instance_lock:
            # Return existing instance if available (singleton per channel)
            if channel in cls._instances:
                instance = cls._instances[channel]
                # Update the name if it changed (same channel, different net name)
                instance._name = name
                instance._pin = pin
                return instance

            # Create new instance and mark as initializing
            instance = super().__new__(cls)
            instance._initializing = threading.Lock()  # Prevent concurrent initialization
            instance._initialized = False
            cls._instances[channel] = instance
            return instance

    def __init__(self, name: str, pin: int | str, location) -> None:
        """Initialize the thermocouple. Skips re-initialization for cached instances."""
        channel = _parse_channel(location)

        # Thread-safe initialization check
        with self._initializing:
            # Prevent re-initialization of cached instances
            if self._initialized:
                return

            super().__init__(name, pin)

            if TemperatureSensor is None:
                raise ThermocoupleBackendError(
                    "Phidget22 not installed on box (TemperatureSensor import failed)"
                )

            self._channel = channel

            # Use file lock to prevent concurrent access across processes during initialization
            with _PhidgetChannelLock(channel, timeout=10.0):
                self.thermocouple = TemperatureSensor()
                self.thermocouple.setChannel(channel)
                self.thermocouple.openWaitForAttachment(5000)

            self._read_lock = threading.Lock()  # Thread-safe reads (within process)
            self._initialized = True

    def read(self) -> float:
        """Read temperature in degrees C (thread-safe and process-safe)."""
        # Cross-process lock to serialize access to hardware
        with _PhidgetChannelLock(self._channel, timeout=10.0):
            # Thread-safe reading within this process
            with self._read_lock:
                value = self.thermocouple.getTemperature()
                return float(value)

    def close(self) -> None:
        """Close the thermocouple connection and release resources."""
        if hasattr(self, 'thermocouple') and self.thermocouple is not None:
            try:
                self.thermocouple.close()
            except Exception:
                pass  # Ignore errors during cleanup
            finally:
                self.thermocouple = None

    def __del__(self) -> None:
        """Cleanup when instance is garbage collected."""
        self.close()

    @classmethod
    def clear_cache(cls) -> None:
        """Clear all cached instances and close connections (useful for testing/cleanup)."""
        with cls._instance_lock:
            for instance in cls._instances.values():
                instance.close()
            cls._instances.clear()


__all__ = ['PhidgetThermocouple', '_parse_channel']
