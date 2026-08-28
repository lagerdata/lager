# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
LabJack UD-series (U3) DAC driver implementing the abstract DACBase interface.

The U3 counterpart to ``labjack_t7.py``. Where the T7 writes a named Modbus
register (``ljm.eWriteName(handle, "DAC0", volts)``) and can read that register
back, the U3 sends a Feedback command carrying raw DAC bits and offers no read
path at all -- see get_voltage below.

Output range is 0.04-4.95 V, not the T7's 0-5 V. The bound is enforced here
rather than inherited, because a value the hardware silently clamps is a
measurement error that surfaces somewhere far away.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from lager.io.dac.dac_net import DACBase

DEBUG = bool(os.environ.get("LAGER_DAC_DEBUG"))

# Datasheet output range for a UD DAC with no load. Writing outside it does not
# fail -- the device clamps -- so the driver refuses instead, which is the only
# way the caller finds out.
MIN_VOLTAGE = 0.04
MAX_VOLTAGE = 4.95

MAX_DAC = 1


def _debug(msg: str) -> None:
    """Debug logging when LAGER_DAC_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"DAC_DEBUG: {msg}\n")
        sys.stderr.flush()


class LabJackUDDACError(RuntimeError):
    """Raised for UD DAC operations the hardware cannot support."""


class LabJackUDDAC(DACBase):
    """
    LabJack UD-series (U3) DAC implementation.

    Provides analog voltage output on DAC0/DAC1. Uses the global UD handle
    manager so ADC, DAC and GPIO on one device share a single USB claim.

    Pin naming:
    - Numeric pins (0-1) are DAC numbers.
    - String pins of the form "DAC0" are accepted and parsed.
    """

    def __init__(self, name: str, pin: int | str,
                 unique_id: Optional[str] = None,
                 model: str = "u3") -> None:
        """
        Initialize UD DAC interface.

        Args:
            name: Human-readable name for this DAC net.
            pin: DAC number (0 or 1), or a name such as "DAC0".
            unique_id: Serial number or scanner VISA address of a specific
                device. None means "first found".
            model: UD model key; see SUPPORTED_MODELS in labjack_ud_handle.
        """
        super().__init__(name, pin)
        from lager.io.labjack_ud_handle import serial_from_address
        self._serial = serial_from_address(unique_id)
        self._model = (model or "u3").lower()
        # Last value this process wrote, per get_voltage's contract below.
        self._last_written: Optional[float] = None

    def _get_device(self):
        """Get the UD device object from the global handle manager."""
        from lager.io.labjack_ud_handle import get_ud_device
        return get_ud_device(self._model, self._serial)

    def _get_dac_number(self) -> int:
        """Convert the pin identifier to a DAC number.

        Raises:
            ValueError: on an unparseable pin or one outside 0-1.
        """
        pin = self._pin
        if isinstance(pin, str):
            text = pin.strip().upper()
            if text.startswith("DAC"):
                text = text[3:]
            try:
                number = int(text)
            except ValueError:
                raise ValueError(
                    f"Invalid LabJack UD DAC pin {pin!r} for net "
                    f"'{self._name}'. Expected 0-{MAX_DAC} or a name like DAC0."
                ) from None
        else:
            number = int(pin)

        if not 0 <= number <= MAX_DAC:
            raise ValueError(
                f"LabJack UD DAC {number} out of range for net "
                f"'{self._name}' (DAC0-DAC{MAX_DAC})."
            )
        return number

    def get_voltage(self) -> float:
        """
        Return the DAC output voltage this process last set.

        The UD API has no DAC readback. The T7 can ``eReadName("DAC0")``
        because a T7 DAC is a Modbus register; a U3 DAC is written by a
        Feedback command and there is no corresponding read.

        So this reports the last value written *through this driver instance*,
        and raises if nothing has been. Returning 0.0 for "unknown" would be
        indistinguishable from a real 0 V reading and would quietly satisfy
        ``DACBase.input()``, whose callers expect a measurement.

        Raises:
            LabJackUDDACError: If this instance has not written a voltage.
        """
        if self._last_written is None:
            raise LabJackUDDACError(
                f"LabJack UD DAC net '{self._name}' has no readback: the "
                f"device provides none, and this process has not written a "
                f"value to report. Set an output first."
            )
        return self._last_written

    def output(self, voltage: float) -> None:
        """
        Set the voltage output of the DAC pin.

        Args:
            voltage: Desired output voltage in volts (0.04-4.95).

        Raises:
            ValueError: If the voltage is outside the device's range.
            RuntimeError: If LabJackPython or the Exodriver is unavailable.
            Exception: For device communication errors.
        """
        voltage = float(voltage)
        if not MIN_VOLTAGE <= voltage <= MAX_VOLTAGE:
            raise ValueError(
                f"Voltage {voltage} V is outside the LabJack UD DAC range "
                f"({MIN_VOLTAGE}-{MAX_VOLTAGE} V) for net '{self._name}'. "
                f"Note this differs from the T7's 0-5 V."
            )

        from lager.io.labjack_ud_handle import load_ud_module

        device = self._get_device()
        dac_number = self._get_dac_number()
        ud = load_ud_module(self._model)

        # 16-bit write. voltageToDACBits applies the device's own calibration
        # constants, which is why the conversion belongs to the device object
        # and not to a constant here.
        bits = device.voltageToDACBits(voltage, dacNumber=dac_number,
                                       is16Bits=True)
        _debug(f"Writing {voltage} V ({bits} bits) to DAC{dac_number} "
               f"for net '{self._name}'")
        device.getFeedback(ud.DAC16(dac_number, bits))
        self._last_written = voltage
