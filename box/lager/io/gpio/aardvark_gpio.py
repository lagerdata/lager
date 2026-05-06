# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Total Phase Aardvark GPIO driver implementing the abstract GPIOBase interface.

Provides digital I/O operations for the Aardvark I2C/SPI Host Adapter's
6 GPIO pins (bits 0-5) on its 10-pin header connector.

Pin mapping (10-pin header):
- Bit 0: SCL  (pin 1)
- Bit 1: SDA  (pin 3)
- Bit 2: MISO (pin 5)
- Bit 3: SCK  (pin 7)
- Bit 4: MOSI (pin 8)
- Bit 5: SS   (pin 9)

NOTE: The Aardvark is configured in GPIO-only mode (AA_CONFIG_GPIO_ONLY = 0x00)
which makes all 6 pins available for GPIO. Since each CLI command runs as a
separate process, this does not conflict with I2C or SPI usage in other commands.
"""
from __future__ import annotations

import atexit
import json
import os
import sys
import time
from typing import Optional

from .gpio_net import GPIOBase
from lager.exceptions import GPIOBackendError

DEBUG = bool(os.environ.get("LAGER_GPIO_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_GPIO_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"GPIO_DEBUG(Aardvark): {msg}\n")
        sys.stderr.flush()


# Maximum retries for USB disconnect recovery
MAX_USB_RETRIES = 3

# Aardvark GPIO-only configuration mode (all 6 pins available)
_AA_CONFIG_GPIO_ONLY = 0x00

# Named pin mapping: signal name -> bit number
_PIN_NAMES = {
    "SCL": 0,
    "SDA": 1,
    "MISO": 2,
    "SCK": 3,
    "MOSI": 4,
    "SS": 5,
}


class AardvarkGPIO(GPIOBase):
    """
    Total Phase Aardvark GPIO implementation using aardvark_py.

    Provides GPIO operations on the Aardvark's 6 GPIO pins (bits 0-5).

    Pin naming:
    - Integer: 0-5
    - String numeric: "0"-"5"
    - String named: "SCL", "SDA", "MISO", "SCK", "MOSI", "SS" (case-insensitive)
    """

    # File-based cache to persist output states across processes
    _CACHE_FILE = "/tmp/aardvark_gpio_cache.json"

    # Class-level handle registry: port -> handle.
    # The Aardvark only allows one open handle per device. Since Net.get()
    # creates a new AardvarkGPIO instance on every call, all instances for
    # the same port must share one handle to avoid error -7 (device in use).
    _shared_handles: dict[int, int] = {}
    _atexit_registered: set[int] = set()

    def __init__(
        self,
        name: str,
        pin: int | str,
        port: int = 0,
        serial: Optional[str] = None,
        target_power: bool = False,
    ) -> None:
        """
        Initialize Aardvark GPIO interface.

        Args:
            name: Human-readable name for this GPIO net
            pin: Pin identifier (0-5, "SCL", "SDA", "MISO", "SCK", "MOSI", "SS")
            port: Aardvark device port number (0 = first device)
            serial: Aardvark serial number string (for multi-device setups).
                    If None, uses the device on the given port.
            target_power: Enable 5V target power from the Aardvark
        """
        super().__init__(name, pin)
        self._port = port
        self._serial = serial
        self._target_power = target_power
        self._handle = None
        self._pin_num = self._parse_pin(pin)

        _debug(f"AardvarkGPIO initialized: name={name}, pin={pin} -> bit {self._pin_num}, "
               f"port={port}, serial={serial}, target_power={target_power}")

    @staticmethod
    def _parse_pin(pin: int | str) -> int:
        """
        Parse a pin identifier to a bit number (0-5).

        Args:
            pin: Integer 0-5, string "0"-"5", or named signal
                 ("SCL", "SDA", "MISO", "SCK", "MOSI", "SS")

        Returns:
            Bit number (0-5)

        Raises:
            GPIOBackendError: If pin identifier is invalid
        """
        # Integer
        if isinstance(pin, int):
            if 0 <= pin <= 5:
                return pin
            raise GPIOBackendError(
                f"Invalid Aardvark GPIO pin number {pin}. Valid range: 0-5 "
                f"(SCL=0, SDA=1, MISO=2, SCK=3, MOSI=4, SS=5)."
            )

        # String
        if isinstance(pin, str):
            pin_stripped = pin.strip()

            # Try numeric string
            try:
                num = int(pin_stripped)
                if 0 <= num <= 5:
                    return num
                raise GPIOBackendError(
                    f"Invalid Aardvark GPIO pin number {num}. Valid range: 0-5 "
                    f"(SCL=0, SDA=1, MISO=2, SCK=3, MOSI=4, SS=5)."
                )
            except ValueError:
                pass

            # Try named pin (case-insensitive)
            upper = pin_stripped.upper()
            if upper in _PIN_NAMES:
                return _PIN_NAMES[upper]

            raise GPIOBackendError(
                f"Invalid Aardvark GPIO pin '{pin}'. "
                f"Use 0-5 or 'SCL'/'SDA'/'MISO'/'SCK'/'MOSI'/'SS'."
            )

        raise GPIOBackendError(
            f"Invalid Aardvark GPIO pin type: {type(pin).__name__}. Expected int or str."
        )

    @staticmethod
    def _get_aa():
        """Lazy-import the aardvark_py module."""
        try:
            import aardvark_py
            return aardvark_py
        except ImportError:
            raise GPIOBackendError(
                "aardvark_py library not available. "
                "Install with: pip install aardvark_py"
            )

    def _ensure_open(self) -> int:
        """
        Open the Aardvark device if not already open.

        Uses a class-level handle registry so all AardvarkGPIO instances
        for the same port share one handle (the Aardvark only allows one
        open handle per device).

        Configures as GPIO-only mode (all 6 pins available), optionally
        enables target power, and restores cached output states.

        Returns the device handle.
        """
        if self._handle is not None:
            return self._handle

        # Check if another instance already opened this port
        shared = AardvarkGPIO._shared_handles.get(self._port)
        if shared is not None:
            self._handle = shared
            _debug(f"Reusing shared handle {shared} for port {self._port}")
            return self._handle

        aa = self._get_aa()

        handle = aa.aa_open(self._port)
        if handle < 0:
            raise GPIOBackendError(
                f"Failed to open Aardvark on port {self._port}: error {handle}"
            )

        # Configure as GPIO-only (all 6 pins available for GPIO)
        result = aa.aa_configure(handle, _AA_CONFIG_GPIO_ONLY)
        _debug(f"aa_open({self._port})={handle}, "
               f"aa_configure(0x{_AA_CONFIG_GPIO_ONLY:02x})={result}")
        if result < 0:
            aa.aa_close(handle)
            raise GPIOBackendError(
                f"Failed to configure Aardvark as GPIO-only: error {result}"
            )

        self._handle = handle
        AardvarkGPIO._shared_handles[self._port] = handle

        # Register atexit only once per port
        if self._port not in AardvarkGPIO._atexit_registered:
            atexit.register(AardvarkGPIO._close_port, self._port)
            AardvarkGPIO._atexit_registered.add(self._port)

        _debug(f"Aardvark opened on port {self._port}, handle={handle}")

        # Enable target power if requested (Aardvark supplies 5V to target)
        if self._target_power:
            tp_fn = getattr(aa, "aa_target_power", None)
            if tp_fn is not None:
                # AA_TARGET_POWER_NONE=0x00, AA_TARGET_POWER_BOTH=0x03
                result = tp_fn(handle, 0x03)
                if result < 0:
                    _debug(f"Target power enable returned error {result}")
                else:
                    _debug("Target power enabled (5V)")
            else:
                _debug("aa_target_power not available in aardvark_py")

        # Restore cached output states so pins persist across processes
        self._restore_cached_outputs()

        return self._handle

    def _restore_cached_outputs(self):
        """
        Restore any previously-cached output pin states for this device.

        Reads the cache file and re-applies direction + value for every
        pin that was last set as an output on this Aardvark device.
        Sets direction BEFORE values (aa_gpio_set only works on output pins).
        """
        aa = self._get_aa()
        handle = self._handle
        device_id = self._serial or f"port_{self._port}"
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
                if not (0 <= pin_num <= 5):
                    continue
            except (ValueError, IndexError):
                continue

            pin_mask = 1 << pin_num
            direction_mask |= pin_mask
            if state.get("value"):
                output_value |= pin_mask

        if direction_mask:
            try:
                # Set direction FIRST so pins are configured as outputs,
                # then set values. aa_gpio_set() only affects pins already
                # configured as outputs -- calling it before direction has
                # no effect on the Aardvark hardware.
                dr = aa.aa_gpio_direction(handle, direction_mask)
                sr = aa.aa_gpio_set(handle, output_value)
                if dr < 0 or sr < 0:
                    _debug(f"Warning: restore cached outputs failed: dir={dr}, set={sr}")
                _debug(f"Restored cached outputs: direction=0x{direction_mask:02x}, "
                       f"value=0x{output_value:02x}")
            except Exception as e:
                _debug(f"Warning: failed to restore cached outputs: {e}")

    @classmethod
    def _close_port(cls, port: int):
        """Close the shared handle for a port (called via atexit)."""
        handle = cls._shared_handles.pop(port, None)
        cls._atexit_registered.discard(port)
        if handle is not None:
            try:
                import aardvark_py
                aardvark_py.aa_close(handle)
                _debug(f"Aardvark GPIO closed (atexit), port={port}, handle={handle}")
            except Exception as e:
                _debug(f"Error closing Aardvark GPIO: {e}")

    def _close(self):
        """Close the Aardvark device and remove the shared handle."""
        if self._handle is not None:
            AardvarkGPIO._shared_handles.pop(self._port, None)
            try:
                aa = self._get_aa()
                aa.aa_close(self._handle)
                _debug(f"Aardvark GPIO closed, handle={self._handle}")
            except Exception as e:
                _debug(f"Error closing Aardvark GPIO: {e}")
            finally:
                self._handle = None

    def _reconnect(self):
        """Close and reopen the Aardvark for USB disconnect recovery."""
        _debug("Attempting reconnect after USB error...")
        # Clear both instance and shared handle so _ensure_open does a fresh open
        AardvarkGPIO._shared_handles.pop(self._port, None)
        self._handle = None
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
        device_id = self._serial or f"port_{self._port}"
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
            GPIOBackendError: If aardvark_py is not available or device error
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
                aa = self._get_aa()
                handle = self._ensure_open()

                # _ensure_open() already called _restore_cached_outputs() which
                # configured all cached output pins via aa_gpio_direction() +
                # aa_gpio_set().  Non-output pins are already inputs (Aardvark
                # default after aa_configure).  Calling aa_gpio_direction()
                # again here would reset the output latch and momentarily drop
                # all output pins to LOW -- breaking loopback reads.

                pins = aa.aa_gpio_get(handle)
                value = 1 if (pins & pin_mask) else 0

                _debug(f"Pin {self._pin_num} input: raw=0x{pins:02x}, value={value}")
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
                    f"Aardvark GPIO input on pin {self._pin_num} failed after "
                    f"{MAX_USB_RETRIES} attempts: {last_error}"
                )

    def output(self, level: int | str) -> None:
        """
        Set the output state of the GPIO pin.

        Builds direction and value masks from cache plus this pin's new state.
        Sets direction BEFORE output value so aa_gpio_set() only affects
        pins already configured as outputs.

        Args:
            level: Output level - accepts int (0/1) or str ("low"/"high", "off"/"on", "0"/"1")

        Raises:
            GPIOBackendError: If aardvark_py is not available or device error
        """
        bit_value = self._parse_level(level)
        pin_mask = 1 << self._pin_num
        last_error = None

        for attempt in range(MAX_USB_RETRIES):
            try:
                aa = self._get_aa()
                handle = self._ensure_open()

                # Build direction + value masks from cache (preserve other output pins)
                device_id = self._serial or f"port_{self._port}"
                cache = self._load_cache()
                direction_mask = 0
                output_value = 0

                for key, state in cache.items():
                    if not key.startswith(device_id + ":"):
                        continue
                    if not state.get("is_output"):
                        continue
                    try:
                        pn = int(key.split(":")[-1])
                        if 0 <= pn <= 5:
                            direction_mask |= (1 << pn)
                            if state.get("value"):
                                output_value |= (1 << pn)
                    except (ValueError, IndexError):
                        continue

                # Add this pin as output with the requested value
                direction_mask |= pin_mask
                if bit_value:
                    output_value |= pin_mask
                else:
                    output_value &= ~pin_mask

                # Set direction FIRST so pins are configured as outputs,
                # then set values. aa_gpio_set() only affects pins already
                # configured as outputs on the Aardvark hardware.
                dir_result = aa.aa_gpio_direction(handle, direction_mask)
                if dir_result < 0:
                    raise GPIOBackendError(
                        f"aa_gpio_direction failed on pin {self._pin_num}: error {dir_result}"
                    )
                set_result = aa.aa_gpio_set(handle, output_value)
                if set_result < 0:
                    raise GPIOBackendError(
                        f"aa_gpio_set failed on pin {self._pin_num}: error {set_result}"
                    )

                # NOTE: aa_gpio_get() does NOT read back output pin states --
                # output pin bits always read as 0.  Readback is omitted to
                # avoid a misleading debug line and save a USB round-trip.
                _debug(f"pin={self._pin_num}, handle={handle}, "
                       f"direction=0x{direction_mask:02x}, value=0x{output_value:02x}, "
                       f"dir_rc={dir_result}, set_rc={set_result}")

                _debug(f"Pin {self._pin_num} output: {bit_value} "
                       f"(direction=0x{direction_mask:02x}, value=0x{output_value:02x})")
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
                    f"Aardvark GPIO output on pin {self._pin_num} failed after "
                    f"{MAX_USB_RETRIES} attempts: {last_error}"
                )

    def __del__(self):
        """Clean up instance reference (shared handle closed via atexit)."""
        self._handle = None
