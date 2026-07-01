# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Yoctopuce watt meter implementation for power measurement.

Uses the Yoctopuce API to interface with Yocto-Watt hardware.
"""

from __future__ import annotations
import threading
import time
from typing import Dict

from .watt_net import WattMeterBase
from lager.exceptions import WattBackendError

try:
    from yoctopuce.yocto_api import YAPI, YRefParam
    from yoctopuce.yocto_power import YPower
except ModuleNotFoundError:
    YAPI = None
    YPower = None
    YRefParam = None


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


class YoctoWatt(WattMeterBase):
    """
    Yoctopuce watt meter implementation with instance caching.

    This implementation uses the Yoctopuce API to read power measurements from
    Yocto-Watt hardware. Instances are cached per channel to enable connection reuse.
    """

    # Class-level cache: one instance per channel for connection reuse
    _instances: Dict[int, 'YoctoWatt'] = {}
    _instance_lock = threading.Lock()
    _yapi_initialized = False
    _yapi_lock = threading.Lock()

    def __new__(cls, name: str, pin: int | str, location) -> 'YoctoWatt':
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

            # Create new instance
            instance = super().__new__(cls)
            instance._initializing = threading.Lock()  # Prevent concurrent initialization
            instance._initialized = False
            cls._instances[channel] = instance
            return instance

    def __init__(self, name: str, pin: int | str, location) -> None:
        """Initialize the watt meter. Skips re-initialization for cached instances."""
        channel = _parse_channel(location)

        # Thread-safe initialization check
        with self._initializing:
            # Prevent re-initialization of cached instances
            if self._initialized:
                return

            super().__init__(name, pin)

            if YAPI is None or YPower is None:
                raise WattBackendError(
                    "Yoctopuce library not installed on box "
                    "(yoctopuce module import failed)"
                )

            self._channel = channel

            # Initialize Yoctopuce API (only once globally)
            with self._yapi_lock:
                if not self._yapi_initialized:
                    errmsg = YRefParam()
                    if YAPI.RegisterHub("usb", errmsg) != YAPI.SUCCESS:
                        raise WattBackendError(
                            f"Failed to initialize Yoctopuce API: {errmsg.value}"
                        )
                    self.__class__._yapi_initialized = True

            # Find the power sensor
            # For channel 0 (default), use first available sensor
            # For channel N, use the Nth sensor found
            sensor = YPower.FirstPower()

            if sensor is None:
                raise WattBackendError(
                    f"No Yocto-Watt power sensor found (channel {channel})"
                )

            # Skip to the correct channel if needed
            for _ in range(channel):
                sensor = sensor.nextPower()
                if sensor is None:
                    raise WattBackendError(
                        f"Yocto-Watt channel {channel} not found "
                        f"(only {channel} sensors detected)"
                    )

            self.sensor = sensor
            self._read_lock = threading.Lock()  # Thread-safe reads
            self._initialized = True

    def read(self, duration: float = 0.1) -> float:
        """
        Read current power in watts (thread-safe).

        Args:
            duration: Accepted for interface compatibility but ignored; the
                Yocto-Watt returns an instantaneous averaged value of its own.

        Returns:
            Power measurement in watts as a float
        """
        with self._read_lock:
            if not self.sensor.isOnline():
                raise WattBackendError(
                    f"Yocto-Watt sensor '{self.name}' is offline or disconnected"
                )

            # Get current power value in watts
            power = self.sensor.get_currentValue()
            return float(power)

    def close(self) -> None:
        """Close the connection and release resources."""
        # Note: We don't call YAPI.FreeAPI() here because it's shared across instances
        # The sensor will be automatically cleaned up by the Yoctopuce API
        self.sensor = None

    def __del__(self) -> None:
        """Cleanup when instance is garbage collected."""
        self.close()

    @classmethod
    def clear_cache(cls) -> None:
        """
        Clear all cached instances and cleanup Yoctopuce API.
        Useful for testing or when you need to reset all connections.
        """
        with cls._instance_lock:
            for instance in cls._instances.values():
                instance.close()
            cls._instances.clear()

            # Clean up Yoctopuce API
            if cls._yapi_initialized and YAPI is not None:
                YAPI.FreeAPI()
                cls._yapi_initialized = False
