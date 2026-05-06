# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
LabJack T7 DAC driver implementing the abstract DACBase interface.

Provides analog voltage output operations for LabJack T7 devices using the
global handle manager for efficient connection sharing with SPI, ADC, and GPIO.
"""

from __future__ import annotations

import os
import sys

from .dac_net import DACBase

DEBUG = bool(os.environ.get("LAGER_DAC_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_DAC_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"DAC_DEBUG: {msg}\n")
        sys.stderr.flush()


class LabJackDAC(DACBase):
    """
    LabJack T7 DAC implementation.

    Provides analog voltage output for LabJack T7 device pins.
    Uses the global LabJack handle manager for efficient connection sharing.

    Pin naming follows LabJack T7 convention:
    - Numeric pins (0-1) map to "DAC0", "DAC1"
    - String pins are used directly as channel names
    """

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
            return f"DAC{pin_num}"
        except (ValueError, TypeError):
            return str(self._pin)

    def get_voltage(self) -> float:
        """
        Read the current voltage output from the DAC pin.

        Returns:
            Current voltage output in volts as a float

        Raises:
            RuntimeError: If LabJack library is not available
            Exception: For LabJack communication errors
        """
        ljm = self._get_ljm()
        handle = self._get_handle()
        channel_name = self._get_channel_name()

        _debug(f"Reading DAC from channel {channel_name}")
        voltage = ljm.eReadName(handle, channel_name)
        return float(voltage)

    def output(self, voltage: float) -> None:
        """
        Set the voltage output of the DAC pin.

        Args:
            voltage: Desired output voltage in volts (0-5V for LabJack T7)

        Raises:
            RuntimeError: If LabJack library is not available
            ValueError: If voltage is out of range (0-5V)
            Exception: For LabJack communication errors
        """
        # Validate voltage range for LabJack T7 DAC (0-5V)
        if voltage < 0 or voltage > 5:
            raise ValueError(f"DAC voltage must be between 0 and 5V, got {voltage}V")

        ljm = self._get_ljm()
        handle = self._get_handle()
        channel_name = self._get_channel_name()

        _debug(f"Writing {voltage}V to channel {channel_name}")
        ljm.eWriteName(handle, channel_name, float(voltage))
