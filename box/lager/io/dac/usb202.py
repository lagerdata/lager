# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
MCC USB-202 DAC driver implementing the abstract DACBase interface.

Provides analog voltage output operations for Measurement Computing USB-202
data acquisition devices using the uldaq library.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from .dac_net import DACBase

DEBUG = bool(os.environ.get("LAGER_DAC_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_DAC_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"DAC_DEBUG: {msg}\n")
        sys.stderr.flush()


# Import uldaq library with error handling
try:
    from uldaq import (
        get_daq_device_inventory,
        InterfaceType,
        Range,
        DaqDevice,
        AOutFlag
    )
    _ULDAQ_ERR = None
except Exception as _exc:
    get_daq_device_inventory = None
    InterfaceType = None
    Range = None
    DaqDevice = None
    AOutFlag = None
    _ULDAQ_ERR = _exc


class USB202DAC(DACBase):
    """
    MCC USB-202 DAC implementation.

    Provides analog voltage output for USB-202 DAQ device channels.
    Each operation opens a connection, performs the output, and closes the connection.

    Channel naming:
    - Channels 0-1 for analog outputs (DAC0, DAC1)
    - 0V to 5V range
    """

    def __init__(self, name: str, pin: int | str, unique_id: Optional[str] = None) -> None:
        """
        Initialize USB-202 DAC interface.

        Args:
            name: Human-readable name for this DAC net
            pin: Channel number (0-1 for USB-202)
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
        self._range = Range.UNI5VOLTS if Range else None

    def _parse_channel(self, pin: int | str) -> int:
        """
        Parse channel identifier to channel number.

        Accepts both numeric (0-1) and name formats (DAC0-DAC1, AOUT0-AOUT1).

        Args:
            pin: Channel identifier (0-1, "DAC0"-"DAC1", or "AOUT0"-"AOUT1")

        Returns:
            Channel number (0-1)

        Raises:
            ValueError: If channel is invalid
        """
        # Try numeric format first
        try:
            channel = int(pin)
            if 0 <= channel <= 1:
                return channel
        except (ValueError, TypeError):
            pass

        # Try name format (DAC0, DAC1, AOUT0, AOUT1)
        if isinstance(pin, str):
            pin_upper = pin.upper().strip()

            # Handle DAC0/DAC1
            if pin_upper.startswith("DAC") and len(pin_upper) >= 4:
                try:
                    channel = int(pin_upper[3:])
                    if 0 <= channel <= 1:
                        return channel
                except ValueError:
                    pass

            # Handle AOUT0/AOUT1
            if pin_upper.startswith("AOUT") and len(pin_upper) >= 5:
                try:
                    channel = int(pin_upper[4:])
                    if 0 <= channel <= 1:
                        return channel
                except ValueError:
                    pass

        # Invalid format
        raise ValueError(
            f"Invalid DAC channel '{pin}'. USB-202 supports channels 0-1, DAC0-DAC1, or AOUT0-AOUT1. "
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

    def output(self, voltage: float) -> None:
        """
        Set the voltage output of the DAC channel.

        Args:
            voltage: Desired output voltage in volts (0.0 to 5.0)

        Raises:
            RuntimeError: If uldaq library is not available
            ValueError: If voltage is out of range
            Exception: For USB-202 communication errors
        """
        if DaqDevice is None:
            raise RuntimeError(f"uldaq library not available: {_ULDAQ_ERR}")

        # Validate voltage range (USB-202 DAC is 0-5V, not bipolar)
        if not (0.0 <= voltage <= 5.0):
            raise ValueError(
                f"Voltage {voltage}V out of range for MCC USB-202 DAC. "
                f"Supported range: 0V to 5V (unipolar). "
                f"Note: Unlike LabJack (±10V), USB-202 DAC cannot output negative voltages."
            )

        device_descriptor = self._get_device()
        daq_device = DaqDevice(device_descriptor)

        try:
            # Connect to device
            _debug(f"Connecting to {device_descriptor.product_name} (ID: {device_descriptor.unique_id})")
            daq_device.connect()

            # Get analog output subsystem
            ao_device = daq_device.get_ao_device()

            # Parse and validate channel
            channel = self._parse_channel(self._pin)

            # Set analog output
            # a_out(channel, range, flags, data)
            _debug(f"Setting channel {channel} to {voltage:.3f}V")
            ao_device.a_out(channel, self._range, AOutFlag.DEFAULT, voltage)

            _debug(f"Channel {channel} set to {voltage:.3f}V")

        finally:
            # Always disconnect and release device
            try:
                if daq_device:
                    _debug("Disconnecting from USB-202")
                    daq_device.disconnect()
                    daq_device.release()
            except Exception as e:
                _debug(f"Error during cleanup: {e}")

    def get_voltage(self) -> float:
        """
        Read the current voltage output from the DAC channel.

        Note: The USB-202 does not support readback of DAC output values.
        This method will raise NotImplementedError.

        Returns:
            Current voltage output in volts

        Raises:
            NotImplementedError: USB-202 does not support DAC readback
        """
        raise NotImplementedError("USB-202 does not support DAC output readback")
