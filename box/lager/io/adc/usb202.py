# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
MCC USB-202 ADC driver implementing the abstract ADCBase interface.

Provides analog voltage measurement operations for Measurement Computing USB-202
data acquisition devices using the uldaq library.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from lager.io.adc.adc_net import ADCBase

DEBUG = bool(os.environ.get("LAGER_ADC_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_ADC_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"ADC_DEBUG: {msg}\n")
        sys.stderr.flush()


# Import uldaq library with error handling
try:
    from uldaq import (
        get_daq_device_inventory,
        InterfaceType,
        AiInputMode,
        Range,
        DaqDevice
    )
    _ULDAQ_ERR = None
except Exception as _exc:
    get_daq_device_inventory = None
    InterfaceType = None
    AiInputMode = None
    Range = None
    DaqDevice = None
    _ULDAQ_ERR = _exc


class USB202ADC(ADCBase):
    """
    MCC USB-202 ADC implementation.

    Provides analog voltage measurement for USB-202 DAQ device channels.
    Each operation opens a connection, performs the measurement, and closes the connection.

    Channel naming:
    - Channels 0-7 for single-ended inputs
    - Supports differential mode if configured
    """

    def __init__(self, name: str, pin: int | str, unique_id: Optional[str] = None) -> None:
        """
        Initialize USB-202 ADC interface.

        Args:
            name: Human-readable name for this ADC net
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
        self._input_mode = AiInputMode.SINGLE_ENDED if AiInputMode else None
        self._range = Range.BIP10VOLTS if Range else None

    def _parse_channel(self, pin: int | str) -> int:
        """
        Parse channel identifier to channel number.

        Accepts both numeric (0-7) and name formats (CH0-CH7).

        Args:
            pin: Channel identifier (0-7 or "CH0"-"CH7")

        Returns:
            Channel number (0-7)

        Raises:
            ValueError: If channel is invalid
        """
        # Try numeric format first
        try:
            channel = int(pin)
            if 0 <= channel <= 7:
                return channel
        except (ValueError, TypeError):
            pass

        # Try name format (CH0, CH1, etc.)
        if isinstance(pin, str):
            pin_upper = pin.upper().strip()
            if pin_upper.startswith("CH") and len(pin_upper) >= 3:
                try:
                    channel = int(pin_upper[2:])
                    if 0 <= channel <= 7:
                        return channel
                except ValueError:
                    pass

        # Invalid format
        raise ValueError(
            f"Invalid ADC channel '{pin}'. USB-202 supports channels 0-7 or CH0-CH7. "
            f"Use 'lager instruments --box <box>' to see available channels."
        )

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

    def input(self) -> float:
        """
        Read the current voltage on the ADC channel.

        Returns:
            Voltage reading in volts as a float

        Raises:
            RuntimeError: If uldaq library is not available
            Exception: For USB-202 communication errors
        """
        if DaqDevice is None:
            raise RuntimeError(f"uldaq library not available: {_ULDAQ_ERR}")

        device_descriptor = self._get_device()
        daq_device = DaqDevice(device_descriptor)

        try:
            # Connect to device
            _debug(f"Connecting to {device_descriptor.product_name} (ID: {device_descriptor.unique_id})")
            daq_device.connect()

            # Get analog input subsystem
            ai_device = daq_device.get_ai_device()

            # Parse and validate channel
            channel = self._parse_channel(self._pin)

            # Read analog input
            # a_in(channel, input_mode, range, flags)
            _debug(f"Reading channel {channel}, mode={self._input_mode}, range={self._range}")
            voltage = ai_device.a_in(channel, self._input_mode, self._range, 0)

            _debug(f"Channel {channel} read: {voltage:.6f} V")
            return float(voltage)

        finally:
            # Always disconnect and release device
            try:
                if daq_device:
                    _debug("Disconnecting from USB-202")
                    daq_device.disconnect()
                    daq_device.release()
            except Exception as e:
                _debug(f"Error during cleanup: {e}")
