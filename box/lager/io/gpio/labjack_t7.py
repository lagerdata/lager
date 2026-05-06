# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
LabJack T7 GPIO driver implementing the abstract GPIOBase interface.

Provides digital I/O operations for LabJack T7 devices using the global
handle manager for efficient connection sharing with SPI, ADC, and DAC.
"""
from __future__ import annotations

import os
import sys
import re
import time
from typing import List

from .gpio_net import GPIOBase

DEBUG = bool(os.environ.get("LAGER_GPIO_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_GPIO_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"GPIO_DEBUG: {msg}\n")
        sys.stderr.flush()


class LabJackGPIO(GPIOBase):
    """
    LabJack T7 GPIO implementation.

    Provides digital I/O operations for LabJack T7 device pins.
    Uses the global LabJack handle manager for efficient connection sharing.

    Pin naming follows LabJack T7 convention:
    - Numeric pins (0-22) map to "FIO0", "FIO1", etc.
    - String pins are used directly as channel names
    """

    def __init__(self, name: str, pin: int | str) -> None:
        super().__init__(name, pin)
        # Register the pin with the conflict tracker so overlapping usage
        # within a single lager-python script produces a warning.
        try:
            from lager.io.labjack_handle import register_labjack_pins
            channel = self._get_channel_name()
            register_labjack_pins("GPIO", {channel: "GPIO"})
        except Exception:
            pass

    def _get_handle(self) -> int:
        """Get LabJack handle from the global handle manager."""
        from lager.io.labjack_handle import get_labjack_handle
        return get_labjack_handle()

    def _get_ljm(self):
        """Get the ljm module from the handle manager."""
        from lager.io.labjack_handle import ljm, _LJM_ERR
        if ljm is None:
            raise RuntimeError(f"LabJack LJM library not available: {_LJM_ERR}")
        return ljm

    def _get_channel_name(self) -> str:
        """Convert pin identifier to LabJack channel name."""
        try:
            pin_num = int(self._pin)
            return f"FIO{pin_num}"
        except (ValueError, TypeError):
            return str(self._pin)

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
        else:
            return 1 if int(level) else 0

    def _get_pin_number(self) -> int | None:
        """
        Extract numeric pin number from pin identifier.

        Returns:
            Pin number as int, or None if not a FIO pin.
        """
        try:
            return int(self._pin)
        except (ValueError, TypeError):
            # Pin might be in "FIO0" format - try to extract the number
            match = re.match(r'FIO(\d+)', str(self._pin), re.IGNORECASE)
            if match:
                return int(match.group(1))
            return None

    def input(self) -> int:
        """
        Read the current state of the GPIO pin.

        Returns:
            0 for LOW, 1 for HIGH

        Raises:
            RuntimeError: If LabJack library is not available
            Exception: For LabJack communication errors
        """
        ljm = self._get_ljm()
        handle = self._get_handle()
        channel_name = self._get_channel_name()

        _debug(f"=== GPIO INPUT START for {channel_name} ===")

        # LabJack T7 FIO pins: Check direction first to avoid reconfiguring the pin
        # - Reading a pin with eReadName() automatically reconfigures it as input
        # - We need to check the direction register and read accordingly

        # For FIO pins, direction is controlled by DIO_DIRECTION register
        # Bit position matches the FIO number (FIO0 = bit 0, FIO1 = bit 1, etc.)
        direction_reg = "DIO_DIRECTION"
        state_reg = "DIO_STATE"

        pin_num = self._get_pin_number()
        if pin_num is None:
            # For non-FIO pins (EIO, CIO, MIO), fall back to direct read
            _debug(f"Non-FIO pin {self._pin}, using direct read")
            value = ljm.eReadName(handle, channel_name)
            int_value = 1 if int(value) else 0
            _debug(f"Read value: {int_value}")
            _debug(f"=== GPIO INPUT END - returning {int_value} ===")
            return int_value

        # Read the direction register
        _debug(f"Reading direction register for FIO{pin_num}...")
        direction_bits = int(ljm.eReadName(handle, direction_reg))
        pin_mask = 1 << pin_num
        is_output = (direction_bits & pin_mask) != 0  # 1 = output, 0 = input
        _debug(f"Direction bits: {direction_bits:016b}, Pin mask: {pin_mask:016b}, Is output: {is_output}")

        if is_output:
            # Pin is configured as output - read the output state register
            # to avoid reconfiguring the pin
            _debug(f"Pin is OUTPUT - reading state register to preserve direction")
            state_bits = int(ljm.eReadName(handle, state_reg))
            int_value = 1 if (state_bits & pin_mask) else 0
            _debug(f"State bits: {state_bits:016b}, Pin value: {int_value}")
        else:
            # Pin is configured as input - safe to read directly
            _debug(f"Pin is INPUT - reading pin value directly")
            value = ljm.eReadName(handle, channel_name)
            int_value = 1 if int(value) else 0
            _debug(f"Read value: {int_value}")

        _debug(f"=== GPIO INPUT END - returning {int_value} ===")
        return int_value

    def output(self, level: int | str) -> None:
        """
        Set the output state of the GPIO pin.

        Args:
            level: Output level - accepts int (0/1) or str ("low"/"high", "off"/"on", "0"/"1")

        Raises:
            RuntimeError: If LabJack library is not available
            Exception: For LabJack communication errors
        """
        ljm = self._get_ljm()
        handle = self._get_handle()
        output_value = self._parse_level(level)
        channel_name = self._get_channel_name()

        # LabJack T7 FIO pins don't have individual direction registers
        # Writing to a pin automatically configures it as output
        _debug(f"Writing {output_value} to channel {channel_name}")
        ljm.eWriteName(handle, channel_name, output_value)

    # ------------------------------------------------------------------
    # Streaming override for wait_for_level
    # ------------------------------------------------------------------

    def wait_for_level(
        self,
        level: int,
        timeout: float | None = None,
        scan_rate: int = 20_000,
        scans_per_read: int = 2,
        **kwargs,
    ) -> float:
        """
        Block until the pin reaches the target level using LabJack streaming.

        Uses ``ljm.eStreamStart`` / ``eStreamRead`` for high-speed
        sampling (default 20 kHz) instead of the polling fallback.

        Args:
            level: Target level (0 or 1).
            timeout: Maximum seconds to wait.  ``None`` means wait forever.
            scan_rate: Stream sample rate in Hz (default 20 000).
            scans_per_read: Number of scans per ``eStreamRead`` call
                            (default 2).  Lower values give faster
                            reaction time but more USB overhead.

        Returns:
            Elapsed time in seconds until the level was detected.

        Raises:
            TimeoutError: If *timeout* is exceeded before the level is seen.
        """
        ljm = self._get_ljm()
        handle = self._get_handle()
        channel_name = self._get_channel_name()

        num_addresses = 1
        a_scan_list = ljm.namesToAddresses(num_addresses, [channel_name])[0]

        _debug(
            f"wait_for_level: channel={channel_name}, level={level}, "
            f"scan_rate={scan_rate}, scans_per_read={scans_per_read}, "
            f"timeout={timeout}"
        )

        actual_rate = ljm.eStreamStart(
            handle, scans_per_read, num_addresses, a_scan_list, scan_rate
        )
        _debug(f"Stream started at {actual_rate} Hz (requested {scan_rate})")

        start = time.monotonic()
        try:
            while True:
                _data, _dev_backlog, _ljm_backlog = ljm.eStreamRead(handle)
                for sample in _data:
                    if (sample >= 0.5) == (level == 1):
                        return time.monotonic() - start
                elapsed = time.monotonic() - start
                if timeout is not None and elapsed >= timeout:
                    raise TimeoutError(
                        f"GPIO '{self._name}' did not reach level {level} "
                        f"within {timeout}s"
                    )
        finally:
            try:
                ljm.eStreamStop(handle)
            except Exception:
                pass
