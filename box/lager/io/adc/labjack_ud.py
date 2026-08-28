# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
LabJack UD-series (U3) ADC driver implementing the abstract ADCBase interface.

The U3 counterpart to ``labjack_t7.py``. It shares no code with that driver
because it shares no library: the T7 reads ``ljm.eReadName(handle, "AIN0")``,
a named Modbus register over LJM, while the U3 calls ``device.getAIN(0)`` over
Exodriver. LJM does not speak to the U3 at all.

Channel numbering on a U3, which is why the mux matters here:

    AIN0-AIN3    FIO0-FIO3   fixed high-voltage inputs on a U3-HV (+/-10.3 V)
    AIN4-AIN7    FIO4-FIO7   flexible, low voltage (0-2.44 V)
    AIN8-AIN15   EIO0-EIO7   flexible, low voltage (0-2.44 V)

An AIN number IS the DIO number of the same physical pin, so AIN5 and FIO5 are
one line in two modes. Reading AIN5 while that line is configured digital
returns a number, not an error -- which is exactly why the mode is set through
the handle manager before every read rather than assumed.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from lager.io.adc.adc_net import ADCBase

DEBUG = bool(os.environ.get("LAGER_ADC_DEBUG"))

# Highest analog input on a U3. Channels 30 (internal temperature) and 31
# (GND) exist in the low-level API but are not exposed as nets: neither is a
# measurement of anything wired to the bench.
MAX_AIN = 15

# Channels below this are the U3-HV's dedicated high-voltage inputs. They have
# no mask bit and need no configuration.
FIRST_FLEXIBLE_AIN = 4


def _debug(msg: str) -> None:
    """Debug logging when LAGER_ADC_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"ADC_DEBUG: {msg}\n")
        sys.stderr.flush()


class LabJackUDADC(ADCBase):
    """
    LabJack UD-series (U3) ADC implementation.

    Provides analog voltage measurement for U3 channels. Uses the global UD
    handle manager so ADC, DAC and GPIO on one device share a single USB claim
    and a single pin-mux configuration.

    Pin naming:
    - Numeric pins (0-15) are AIN channel numbers.
    - String pins of the form "AIN5" are accepted and parsed.
    """

    def __init__(self, name: str, pin: int | str,
                 unique_id: Optional[str] = None,
                 model: str = "u3") -> None:
        """
        Initialize UD ADC interface.

        Args:
            name: Human-readable name for this ADC net.
            pin: AIN channel number, or a name such as "AIN5".
            unique_id: Serial number or scanner VISA address of a specific
                device. None means "first found", which is correct for a box
                with one UD device and ambiguous for a box with two.
            model: UD model key; see SUPPORTED_MODELS in labjack_ud_handle.
        """
        super().__init__(name, pin)
        from lager.io.labjack_ud_handle import serial_from_address
        self._serial = serial_from_address(unique_id)
        self._model = (model or "u3").lower()

    def _get_device(self):
        """Get the UD device object from the global handle manager."""
        from lager.io.labjack_ud_handle import get_ud_device
        return get_ud_device(self._model, self._serial)

    def _get_channel(self) -> int:
        """Convert the pin identifier to an AIN channel number.

        Raises:
            ValueError: on an unparseable pin or one outside 0-15. Guessing
                would read a different channel and report it as this net.
        """
        pin = self._pin
        if isinstance(pin, str):
            text = pin.strip().upper()
            if text.startswith("AIN"):
                text = text[3:]
            try:
                channel = int(text)
            except ValueError:
                raise ValueError(
                    f"Invalid LabJack UD ADC pin {pin!r} for net "
                    f"'{self._name}'. Expected 0-{MAX_AIN} or a name like AIN5."
                ) from None
        else:
            channel = int(pin)

        if not 0 <= channel <= MAX_AIN:
            raise ValueError(
                f"LabJack UD ADC channel {channel} out of range for net "
                f"'{self._name}' (AIN0-AIN{MAX_AIN})."
            )
        return channel

    def input(self) -> float:
        """
        Read the current voltage on the ADC pin.

        Returns:
            Voltage reading in volts as a float.

        Raises:
            ValueError: If the pin is not a valid AIN channel.
            RuntimeError: If LabJackPython or the Exodriver is unavailable.
            Exception: For device communication errors.
        """
        from lager.io.labjack_ud_handle import set_channel_mode

        device = self._get_device()
        channel = self._get_channel()

        # AIN0-3 on a U3-HV are permanently analog and have no mask bit; the
        # flexible channels must be switched out of digital mode first. The
        # manager memoizes, so this is a no-op after the first read.
        if channel >= FIRST_FLEXIBLE_AIN:
            set_channel_mode(device, channel, analog=True)

        _debug(f"Reading AIN{channel} on {self._model} for net '{self._name}'")
        voltage = device.getAIN(channel)
        return float(voltage)
