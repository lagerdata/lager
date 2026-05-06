# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
FTDI FT232H GPIO driver implementing the abstract GPIOBase interface.

Provides digital I/O operations for the FT232H USB-to-MPSSE adapter
(VID:PID 0403:6014) using the pyftdi library's GpioAsyncController.

Pin naming follows the FT232H convention:
- ADBUS pins: AD0-AD7 (numbers 0-7)
- ACBUS pins: AC0-AC7 (numbers 8-15)

NOTE: The FT232H's GPIO mode claims the entire FTDI interface.
It cannot be shared simultaneously with I2C or SPI on the same device.
Use a separate FT232H or run commands sequentially.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import sys
import time
from typing import Optional

from .gpio_net import GPIOBase
from lager.exceptions import GPIOBackendError

DEBUG = bool(os.environ.get("LAGER_GPIO_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_GPIO_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"GPIO_DEBUG(FT232H): {msg}\n")
        sys.stderr.flush()


# Maximum retries for USB disconnect recovery
MAX_USB_RETRIES = 3

# Named pin mapping: AD0-AD7 -> 0-7, AC0-AC7 -> 8-15
_PIN_NAMES = {}
for _i in range(8):
    _PIN_NAMES[f"AD{_i}"] = _i
    _PIN_NAMES[f"AC{_i}"] = _i + 8


class FT232HGPIO(GPIOBase):
    """
    FTDI FT232H GPIO implementation using pyftdi GpioAsyncController.

    Provides GPIO operations on ADBUS (AD0-AD7) pins.

    Pin naming:
    - Integer: 0-7 (AD0-AD7), 8-15 (AC0-AC7)
    - String numeric: "4", "12"
    - String named: "AD0"-"AD7", "AC0"-"AC7"
    """

    # File-based cache to persist output states across processes
    _CACHE_FILE = "/tmp/ft232h_gpio_cache.json"

    def __init__(
        self,
        name: str,
        pin: int | str,
        serial: Optional[str] = None,
    ) -> None:
        """
        Initialize FT232H GPIO interface.

        Args:
            name: Human-readable name for this GPIO net
            pin: Pin identifier (0-15, "AD0"-"AD7", "AC0"-"AC7")
            serial: USB serial number string (for multi-device setups).
                    If None, uses the first available FT232H.
        """
        super().__init__(name, pin)
        self._serial = serial
        self._controller = None
        self._pin_num = self._parse_pin(pin)

        _debug(f"FT232HGPIO initialized: name={name}, pin={pin} -> bit {self._pin_num}, "
               f"serial={serial}")

    @staticmethod
    def _parse_pin(pin: int | str) -> int:
        """
        Parse a pin identifier to a bit number (0-15).

        Args:
            pin: Integer 0-15, string "0"-"15", or named "AD0"-"AD7"/"AC0"-"AC7"

        Returns:
            Bit number (0-15)

        Raises:
            GPIOBackendError: If pin identifier is invalid
        """
        # Integer
        if isinstance(pin, int):
            if 0 <= pin <= 15:
                return pin
            raise GPIOBackendError(
                f"Invalid FT232H pin number {pin}. Valid range: 0-15 "
                f"(0-7 for AD0-AD7, 8-15 for AC0-AC7)."
            )

        # String
        if isinstance(pin, str):
            pin_stripped = pin.strip()

            # Try numeric string
            try:
                num = int(pin_stripped)
                if 0 <= num <= 15:
                    return num
                raise GPIOBackendError(
                    f"Invalid FT232H pin number {num}. Valid range: 0-15 "
                    f"(0-7 for AD0-AD7, 8-15 for AC0-AC7)."
                )
            except ValueError:
                pass

            # Try named pin (AD0-AD7, AC0-AC7)
            upper = pin_stripped.upper()
            if upper in _PIN_NAMES:
                return _PIN_NAMES[upper]

            raise GPIOBackendError(
                f"Invalid FT232H pin '{pin}'. Use 0-15, 'AD0'-'AD7', or 'AC0'-'AC7'."
            )

        raise GPIOBackendError(
            f"Invalid FT232H pin type: {type(pin).__name__}. Expected int or str."
        )

    @staticmethod
    def _get_pyftdi():
        """Lazy-import the pyftdi GPIO module."""
        try:
            from pyftdi.gpio import GpioAsyncController
            return GpioAsyncController
        except ImportError:
            raise GPIOBackendError(
                "pyftdi library not available. "
                "Install with: pip install pyftdi"
            )

    def _build_url(self) -> str:
        """Build the pyftdi device URL."""
        if self._serial:
            return f"ftdi://ftdi:232h:{self._serial}/1"
        return "ftdi://ftdi:232h/1"

    def _ensure_open(self):
        """
        Open the FT232H GPIO controller if not already open.

        After opening, restores any cached output pin states so that
        output levels persist across CLI command processes.

        Returns the GpioAsyncController instance.
        """
        if self._controller is not None:
            return self._controller

        GpioAsyncController = self._get_pyftdi()

        url = self._build_url()
        _debug(f"Opening FT232H GPIO at URL: {url}")

        try:
            ctrl = GpioAsyncController()
            # Configure with all pins as inputs initially (direction=0)
            ctrl.configure(url, direction=0)
        except Exception as exc:
            raise GPIOBackendError(
                f"Failed to open FT232H GPIO at {url}: {exc}"
            ) from exc

        self._controller = ctrl
        atexit.register(self._close)

        # If serial wasn't provided via net config, detect it from the
        # actual USB device.  This ensures the cache key matches regardless
        # of whether the net record carried the serial in its address field.
        if not self._serial:
            try:
                self._serial = ctrl._ftdi.usb_dev.serial_number
                _debug(f"Detected FT232H serial: {self._serial}")
            except Exception:
                _debug("Could not detect FT232H serial, using default")

        _debug("FT232H GPIO opened (all pins as inputs)")

        # Restore cached output states so pins persist across processes
        self._restore_cached_outputs(ctrl)

        return self._controller

    def _restore_cached_outputs(self, ctrl):
        """
        Restore any previously-cached output pin states for this device.

        Reads the cache file and re-applies direction + value for every
        pin that was last set as an output on this FT232H device.
        """
        device_id = self._serial or "default_ft232h"
        cache = self._load_cache()

        direction_mask = 0
        output_value = 0

        for key, state in cache.items():
            if not key.startswith(device_id + ":"):
                continue
            if not state.get("is_output"):
                continue
            try:
                pin_num = int(key.split(":")[-1])
                if not (0 <= pin_num <= 15):
                    continue
            except (ValueError, IndexError):
                continue

            pin_mask = 1 << pin_num
            direction_mask |= pin_mask
            if state.get("value"):
                output_value |= pin_mask

        if direction_mask:
            try:
                # Write output values BEFORE enabling pins as outputs.
                # After a USB reset the FTDI output latch is undefined (often
                # 0xFF / all-HIGH).  If we set_direction first, the pin
                # briefly drives the stale latch value until write() corrects
                # it -- causing loopback reads on adjacent pins to see HIGH
                # instead of LOW.
                ctrl.write(output_value)
                ctrl.set_direction(direction_mask, direction_mask)
                _debug(f"Restored cached outputs: direction=0x{direction_mask:04x}, "
                       f"value=0x{output_value:04x}")
            except Exception as e:
                _debug(f"Warning: failed to restore cached outputs: {e}")

    def _close(self):
        """Close the FT232H GPIO controller."""
        if self._controller is not None:
            try:
                self._controller.close()
                _debug("FT232H GPIO closed")
            except Exception as e:
                _debug(f"Error closing FT232H GPIO: {e}")
            finally:
                self._controller = None

    def _reconnect(self):
        """Close and reopen the FT232H for USB disconnect recovery."""
        _debug("Attempting reconnect after USB error...")
        self._controller = None
        self._ensure_open()

    def _parse_level(self, level: int | str) -> int:
        """
        Parse level input to 0 or 1.

        Args:
            level: String or integer level specification

        Returns:
            0 for LOW, 1 for HIGH
        """
        if isinstance(level, str):
            level_str = level.strip().lower()
            return 1 if level_str in ("1", "on", "high", "true") else 0
        return 1 if int(level) else 0

    # ---------- file-based state cache ----------

    def _get_cache_key(self) -> str:
        """Get cache key for this pin."""
        device_id = self._serial or "default_ft232h"
        return f"{device_id}:{self._pin_num}"

    def _load_cache(self) -> dict:
        """Load cache from file."""
        try:
            with open(self._CACHE_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_cache(self, cache: dict) -> None:
        """Save cache to file."""
        try:
            with open(self._CACHE_FILE, "w") as f:
                json.dump(cache, f)
        except Exception as e:
            _debug(f"Warning: Failed to save cache: {e}")

    def _get_cached_state(self) -> dict:
        """Get cached state for this pin."""
        cache = self._load_cache()
        key = self._get_cache_key()
        return cache.get(key, {"value": None, "is_output": False})

    def _set_cached_state(self, value: int, is_output: bool) -> None:
        """Update cached state for this pin."""
        cache = self._load_cache()
        key = self._get_cache_key()
        cache[key] = {"value": value, "is_output": is_output}
        self._save_cache(cache)

    # ---------- GPIO operations ----------

    def input(self) -> int:
        """
        Read the current state of the GPIO pin.

        If this pin was last configured as an output (cached), returns the
        cached output value without changing pin direction.  This prevents
        destroying the output drive state when reading back an output pin.

        For a pure input pin (not previously set as output), sets the pin
        direction to input and reads the physical level.

        Returns:
            0 for LOW, 1 for HIGH

        Raises:
            GPIOBackendError: If pyftdi is not available or device error
        """
        # If this pin is cached as an output, return the cached value
        # instead of changing direction (which would lose the output state).
        cached = self._get_cached_state()
        if cached.get("is_output") and cached.get("value") is not None:
            value = int(cached["value"])
            _debug(f"Pin {self._pin_num} input (from cache, pin is output): value={value}")
            return value

        pin_mask = 1 << self._pin_num
        last_error = None

        for attempt in range(MAX_USB_RETRIES):
            try:
                ctrl = self._ensure_open()

                # Set this pin as input
                ctrl.set_direction(pin_mask, 0)

                # Read all pins
                pins = ctrl.read()
                value = 1 if (pins & pin_mask) else 0

                _debug(f"Pin {self._pin_num} input: raw=0x{pins:04x}, value={value}")
                self._set_cached_state(value, is_output=False)
                return value

            except GPIOBackendError:
                raise
            except Exception as e:
                last_error = e
                _debug(f"USB error on input attempt {attempt + 1}/{MAX_USB_RETRIES}: {e}")
                if attempt < MAX_USB_RETRIES - 1:
                    time.sleep(1.0 * (2 ** attempt))
                    self._reconnect()
                    continue
                raise GPIOBackendError(
                    f"FT232H GPIO input on pin {self._pin_num} failed after "
                    f"{MAX_USB_RETRIES} attempts: {last_error}"
                )

    def output(self, level: int | str) -> None:
        """
        Set the output state of the GPIO pin.

        Uses read-modify-write to avoid clobbering other pins' output state.

        Args:
            level: Output level - accepts int (0/1) or str ("low"/"high", "off"/"on", "0"/"1")

        Raises:
            GPIOBackendError: If pyftdi is not available or device error
        """
        bit_value = self._parse_level(level)
        pin_mask = 1 << self._pin_num
        last_error = None

        for attempt in range(MAX_USB_RETRIES):
            try:
                ctrl = self._ensure_open()

                # Read-modify-write: set the value in the output latch
                # BEFORE enabling the pin as output to avoid a brief glitch
                # where the pin drives a stale latch value (often 0xFF after
                # USB reset).
                current_pins = ctrl.read()
                if bit_value:
                    new_pins = current_pins | pin_mask
                else:
                    new_pins = current_pins & ~pin_mask

                ctrl.write(new_pins)
                ctrl.set_direction(pin_mask, pin_mask)

                _debug(f"Pin {self._pin_num} output: {bit_value} "
                       f"(pins 0x{current_pins:04x} -> 0x{new_pins:04x})")
                self._set_cached_state(bit_value, is_output=True)
                return

            except GPIOBackendError:
                raise
            except Exception as e:
                last_error = e
                _debug(f"USB error on output attempt {attempt + 1}/{MAX_USB_RETRIES}: {e}")
                if attempt < MAX_USB_RETRIES - 1:
                    time.sleep(1.0 * (2 ** attempt))
                    self._reconnect()
                    continue
                raise GPIOBackendError(
                    f"FT232H GPIO output on pin {self._pin_num} failed after "
                    f"{MAX_USB_RETRIES} attempts: {last_error}"
                )

    def __del__(self):
        """Clean up: close device on garbage collection."""
        self._close()
