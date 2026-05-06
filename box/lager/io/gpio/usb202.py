# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
MCC USB-202 GPIO driver implementing the abstract GPIOBase interface.

Provides digital I/O operations for Measurement Computing USB-202
data acquisition devices using the uldaq library.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from .gpio_net import GPIOBase

DEBUG = bool(os.environ.get("LAGER_GPIO_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_GPIO_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"GPIO_DEBUG: {msg}\n")
        sys.stderr.flush()


# Import uldaq library with error handling
try:
    from uldaq import (
        get_daq_device_inventory,
        InterfaceType,
        DaqDevice,
        DigitalDirection,
        DigitalPortType
    )
    _ULDAQ_ERR = None
except Exception as _exc:
    get_daq_device_inventory = None
    InterfaceType = None
    DaqDevice = None
    DigitalDirection = None
    DigitalPortType = None
    _ULDAQ_ERR = _exc


class USB202GPIO(GPIOBase):
    """
    MCC USB-202 GPIO implementation.

    Provides digital I/O for USB-202 DAQ device pins.
    Each operation opens a connection, performs the I/O, and closes the connection.

    Pin naming:
    - Channels 0-7 for digital I/O (DIO0-DIO7)
    - TTL-level (0-5V logic)
    - Each pin independently configurable as input or output
    """

    # File-based cache to persist across processes/threads
    # Stored in /tmp since it's ephemeral state that doesn't need to survive reboots
    _CACHE_FILE = "/tmp/usb202_gpio_cache.json"

    def __init__(self, name: str, pin: int | str, unique_id: Optional[str] = None) -> None:
        """
        Initialize USB-202 GPIO interface.

        Args:
            name: Human-readable name for this GPIO net
            pin: Channel number (0-7 for USB-202)
            unique_id: Optional unique ID or VISA address of specific USB-202 device
                      Can be either a serial number (e.g., "0252829E") or
                      VISA address (e.g., "USB0::0x09DB::0x012B::0252829E::INSTR")
        """
        super().__init__(name, pin)
        # Parse unique_id - extract serial number from VISA address if needed
        if unique_id and "::" in unique_id:
            # VISA address format: USB0::0x09DB::0x012B::SERIAL::INSTR
            parts = unique_id.split("::")
            if len(parts) >= 4:
                self._unique_id = parts[3]  # Extract serial number
            else:
                self._unique_id = unique_id
        else:
            self._unique_id = unique_id

    def _get_device(self):
        """
        Get the USB-202 device descriptor.

        Returns:
            Device descriptor for the USB-202

        Raises:
            RuntimeError: If uldaq library not available or device not found
        """
        if get_daq_device_inventory is None:
            raise RuntimeError(f"uldaq library not available: {_ULDAQ_ERR}")

        devices = get_daq_device_inventory(InterfaceType.USB)

        if not devices:
            raise RuntimeError("No MCC DAQ devices found on USB bus")

        # If specific unique_id specified, find that device
        if self._unique_id:
            for device in devices:
                if device.unique_id == self._unique_id:
                    return device
            raise RuntimeError(f"USB-202 device with unique_id {self._unique_id} not found")

        # Otherwise, find first USB-202
        for device in devices:
            if 'USB-202' in device.product_name or device.product_id == 299:
                return device

        # Fallback: use first available device
        _debug(f"Warning: No USB-202 found, using first available device: {devices[0].product_name}")
        return devices[0]

    def _get_cache_key(self) -> tuple:
        """Get cache key for this pin (device_id, pin_number)."""
        import os
        # Use unique_id if available, otherwise use a default key
        device_id = self._unique_id or "default_usb202"
        # Parse pin to get consistent numeric key
        try:
            pin_num = self._parse_channel(self._pin)
        except ValueError:
            pin_num = str(self._pin)

        key = (device_id, pin_num)
        _debug(f"PID={os.getpid()}, Cache key: {key}, raw pin: {self._pin}, unique_id: {self._unique_id}")
        return key

    def _load_cache_from_file(self) -> dict:
        """Load cache from file. Returns empty dict if file doesn't exist or is invalid."""
        import json
        try:
            with open(self._CACHE_FILE, 'r') as f:
                cache = json.load(f)
                # Convert string keys back to tuples
                return {eval(k): v for k, v in cache.items()}
        except (FileNotFoundError, json.JSONDecodeError, Exception):
            return {}

    def _save_cache_to_file(self, cache: dict) -> None:
        """Save cache to file."""
        import json
        try:
            # Convert tuple keys to strings for JSON
            cache_str_keys = {str(k): v for k, v in cache.items()}
            with open(self._CACHE_FILE, 'w') as f:
                json.dump(cache_str_keys, f)
        except Exception as e:
            _debug(f"Warning: Failed to save cache to file: {e}")

    def _get_cached_state(self) -> dict:
        """Get cached state for this pin from file."""
        cache = self._load_cache_from_file()
        key = self._get_cache_key()
        state = cache.get(key, {'value': None, 'is_output': False})
        _debug(f"GET cache: key={key}, state={state}, cache_size={len(cache)}")
        return state

    def _set_cached_state(self, value: int, is_output: bool) -> None:
        """Update cached state for this pin in file."""
        cache = self._load_cache_from_file()
        key = self._get_cache_key()
        cache[key] = {'value': value, 'is_output': is_output}
        self._save_cache_to_file(cache)
        _debug(f"SET cache: key={key}, value={value}, is_output={is_output}, cache_size={len(cache)}")

    def _parse_channel(self, pin: int | str) -> int:
        """
        Parse channel identifier to bit number.

        Accepts both numeric (0-7) and name formats (DIO0-DIO7).

        Args:
            pin: Channel identifier (0-7 or "DIO0"-"DIO7")

        Returns:
            Bit number (0-7)

        Raises:
            ValueError: If channel is invalid
        """
        # Try numeric format first
        try:
            bit = int(pin)
            if 0 <= bit <= 7:
                return bit
        except (ValueError, TypeError):
            pass

        # Try name format (DIO0, DIO1, etc.)
        if isinstance(pin, str):
            pin_upper = pin.upper().strip()
            if pin_upper.startswith("DIO") and len(pin_upper) >= 4:
                try:
                    bit = int(pin_upper[3:])
                    if 0 <= bit <= 7:
                        return bit
                except ValueError:
                    pass

        # Invalid format
        raise ValueError(
            f"Invalid GPIO channel '{pin}'. USB-202 supports channels 0-7 or DIO0-DIO7. "
            f"Use 'lager instruments --box <box>' to see available channels."
        )

    def input(self) -> int:
        """
        Read the current state of the GPIO pin.

        If the pin is in output mode, returns the last output value from cache.
        If in input mode, reads the actual pin state.

        Returns:
            0 for LOW/False, 1 for HIGH/True

        Raises:
            RuntimeError: If uldaq library is not available
            Exception: For USB-202 communication errors
        """
        # Get cached state
        cached = self._get_cached_state()

        # If pin is in output mode, return cached value
        # (Can't reliably read output pins without reconfiguring)
        if cached['is_output'] and cached['value'] is not None:
            _debug(f"Returning cached output value: {cached['value']}")
            return cached['value']

        if DaqDevice is None:
            raise RuntimeError(f"uldaq library not available: {_ULDAQ_ERR}")

        device_descriptor = self._get_device()
        daq_device = DaqDevice(device_descriptor)

        try:
            # Connect to device
            _debug(f"Connecting to {device_descriptor.product_name} (ID: {device_descriptor.unique_id})")
            daq_device.connect()

            # Get digital I/O subsystem
            dio_device = daq_device.get_dio_device()

            # Parse and validate channel
            bit_num = self._parse_channel(self._pin)

            # Configure pin as input
            # d_config_bit(port_type, bit_number, direction)
            dio_device.d_config_bit(DigitalPortType.AUXPORT, bit_num, DigitalDirection.INPUT)

            # Read bit value
            # d_bit_in(port_type, bit_number)
            value = dio_device.d_bit_in(DigitalPortType.AUXPORT, bit_num)

            _debug(f"Pin {bit_num} read: {value}")

            # Update cache to reflect input mode
            self._set_cached_state(int(value), is_output=False)

            return int(value)

        finally:
            # Always disconnect and release device
            try:
                if daq_device:
                    _debug("Disconnecting from USB-202")
                    daq_device.disconnect()
                    daq_device.release()
            except Exception as e:
                _debug(f"Error during cleanup: {e}")

    def output(self, level: int | str) -> None:
        """
        Set the output state of the GPIO pin.

        Args:
            level: Output level - accepts:
                   - int: 0 = LOW, non-zero = HIGH
                   - str: "0"/"low"/"off" = LOW, "1"/"high"/"on" = HIGH

        Raises:
            RuntimeError: If uldaq library is not available
            Exception: For USB-202 communication errors
        """
        if DaqDevice is None:
            raise RuntimeError(f"uldaq library not available: {_ULDAQ_ERR}")

        # Parse level
        if isinstance(level, str):
            level_lower = level.lower()
            if level_lower in ("0", "low", "off", "false"):
                bit_value = 0
            elif level_lower in ("1", "high", "on", "true"):
                bit_value = 1
            else:
                raise ValueError(f"Invalid level string: {level}. Use '0'/'low'/'off' or '1'/'high'/'on'")
        else:
            bit_value = 1 if level else 0

        device_descriptor = self._get_device()
        daq_device = DaqDevice(device_descriptor)

        try:
            # Connect to device
            _debug(f"Connecting to {device_descriptor.product_name} (ID: {device_descriptor.unique_id})")
            daq_device.connect()

            # Get digital I/O subsystem
            dio_device = daq_device.get_dio_device()

            # Parse and validate channel
            bit_num = self._parse_channel(self._pin)

            # Configure pin as output
            # d_config_bit(port_type, bit_number, direction)
            dio_device.d_config_bit(DigitalPortType.AUXPORT, bit_num, DigitalDirection.OUTPUT)

            # Write bit value
            # d_bit_out(port_type, bit_number, bit_value)
            dio_device.d_bit_out(DigitalPortType.AUXPORT, bit_num, bit_value)

            _debug(f"Pin {bit_num} set to {bit_value}")

            # Save state to cache for toggle functionality
            self._set_cached_state(bit_value, is_output=True)

        finally:
            # Always disconnect and release device
            try:
                if daq_device:
                    _debug("Disconnecting from USB-202")
                    daq_device.disconnect()
                    daq_device.release()
            except Exception as e:
                _debug(f"Error during cleanup: {e}")
