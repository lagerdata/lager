# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
LabJack T7 ADC driver implementing the abstract ADCBase interface.

Provides analog voltage measurement operations for LabJack T7 devices using the
global handle manager for efficient connection sharing with SPI, DAC, and GPIO.
"""

from __future__ import annotations

import os
import sys

from lager.io.adc.adc_net import ADCBase

DEBUG = bool(os.environ.get("LAGER_ADC_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_ADC_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"ADC_DEBUG: {msg}\n")
        sys.stderr.flush()


class LabJackADC(ADCBase):
    """
    LabJack T7 ADC implementation.

    Provides analog voltage measurement for LabJack T7 device pins.
    Uses the global LabJack handle manager for efficient connection sharing.

    Pin naming follows LabJack T7 convention:
    - Numeric pins (0-13) map to "AIN0", "AIN1", etc.
    - String pins are used directly as channel names
    """

    # (handle_id, channel_name) pairs we have already configured this process.
    # LabJack T7 AIN_* register state is sticky in device RAM until the device
    # is power-cycled, so we only need to write the config once per handle.
    _configured: set = set()

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
            return f"AIN{pin_num}"
        except (ValueError, TypeError):
            return str(self._pin)

    def _ensure_configured(self, ljm, handle: int, channel_name: str) -> None:
        """Write safe-default AIN configuration once per (handle, channel).

        LabJack T7 AIN_RANGE / AIN_NEGATIVE_CH / AIN_RESOLUTION_INDEX /
        AIN_SETTLING_US settings persist in device RAM across processes and
        sessions until the device is power-cycled. Without explicit
        configuration, the driver inherits whatever a previous tool (legacy
        gateway container, calibration utility, debug session, etc.) wrote.

        In particular, if a previous tool left an AIN in differential mode
        with a floating negative channel, the read saturates to ~10.10 V
        regardless of the actual pin signal -- producing a fake "open input"
        signature that's indistinguishable from a real wiring fault.

        Apply the safe defaults explicitly:
            RANGE            = 10.0  (+/-10 V single-ended span)
            NEGATIVE_CH      = 199   (use internal GND as reference)
            RESOLUTION_INDEX = 0     (device default; ~16-bit)
            SETTLING_US      = 0     (auto-pick based on resolution)

        Config-write failures are logged but not raised; falling back to
        whatever state the device is currently in is safer than blocking
        the read entirely.
        """
        key = (handle, channel_name)
        if key in LabJackADC._configured:
            return
        try:
            ljm.eWriteName(handle, f"{channel_name}_RANGE", 10.0)
            ljm.eWriteName(handle, f"{channel_name}_NEGATIVE_CH", 199)
            ljm.eWriteName(handle, f"{channel_name}_RESOLUTION_INDEX", 0)
            ljm.eWriteName(handle, f"{channel_name}_SETTLING_US", 0)
            LabJackADC._configured.add(key)
            _debug(f"Configured {channel_name}: single-ended +/-10V, GND ref, default resolution")
        except Exception as e:
            _debug(f"AIN config for {channel_name} failed (continuing with current device state): {e}")

    def input(self) -> float:
        """
        Read the current voltage on the ADC pin.

        Returns:
            Voltage reading in volts as a float

        Raises:
            RuntimeError: If LabJack library is not available
            Exception: For LabJack communication errors
        """
        ljm = self._get_ljm()
        handle = self._get_handle()
        channel_name = self._get_channel_name()

        self._ensure_configured(ljm, handle, channel_name)

        _debug(f"Reading ADC from channel {channel_name}")
        voltage = ljm.eReadName(handle, channel_name)
        return float(voltage)
