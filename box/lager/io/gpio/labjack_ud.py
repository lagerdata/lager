# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
LabJack UD-series (U3) GPIO driver implementing the abstract GPIOBase
interface.

The U3 counterpart to ``labjack_t7.py``, and deliberately much shorter than it
for two reasons worth stating, because "the T7 does this, so we should too" is
the wrong instinct in both cases:

**No direction dance.** The T7 driver reads ``DIO_DIRECTION`` before
``DIO_STATE`` because on a T7 an ``eReadName`` of the pin reconfigures it as an
input -- reading would change the thing being measured. The U3's
``BitStateRead`` has no such side effect ("read the state of a single bit of
digital I/O"), while ``BitStateWrite`` forces the line to output on its own.
So a read is one command and a write is one command.

**No streaming.** The T7 overrides ``wait_for_level`` with an LJM stream
(``eStreamStart``/``eStreamRead``) for microsecond edge capture. LJM does not
talk to the U3, and the UD stream API is a different shape entirely, so this
driver inherits ``GPIOBase.wait_for_level``'s polling loop rather than pretend
to an accuracy it does not have.

Pin availability on a U3-HV is narrower than a T7's, and the difference is not
cosmetic: FIO0-FIO3 do not exist as digital lines at all -- they are the fixed
high-voltage analog inputs. Usable lines are FIO4-FIO7, EIO0-EIO7, CIO0-CIO3.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from lager.io.gpio.gpio_net import GPIOBase

DEBUG = bool(os.environ.get("LAGER_GPIO_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_GPIO_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"GPIO_DEBUG: {msg}\n")
        sys.stderr.flush()


class LabJackUDGPIO(GPIOBase):
    """
    LabJack UD-series (U3) GPIO implementation.

    Provides digital I/O for U3 lines. Uses the global UD handle manager so
    ADC, DAC and GPIO on one device share a single USB claim and a single
    pin-mux configuration.

    Pin naming:
    - String pins such as "FIO4", "EIO0", "CIO2" are the normal form.
    - Numeric pins are DIO numbers (FIO0-7 = 0-7, EIO0-7 = 8-15,
      CIO0-3 = 16-19).
    """

    def __init__(self, name: str, pin: int | str,
                 unique_id: Optional[str] = None,
                 model: str = "u3") -> None:
        """
        Initialize UD GPIO interface.

        Args:
            name: Human-readable name for this GPIO net.
            pin: Pin name such as "FIO4", or a DIO number.
            unique_id: Serial number or scanner VISA address of a specific
                device. None means "first found".
            model: UD model key; see SUPPORTED_MODELS in labjack_ud_handle.
        """
        super().__init__(name, pin)
        from lager.io.labjack_ud_handle import serial_from_address
        self._serial = serial_from_address(unique_id)
        self._model = (model or "u3").lower()

        # Same cross-subsystem conflict tracker the T7 driver uses. It keys on
        # pin-name strings and knows nothing about LJM, so it is shared rather
        # than reimplemented.
        try:
            from lager.io.labjack_handle import register_labjack_pins
            from lager.io.labjack_ud_handle import dio_to_pin, pin_to_dio
            register_labjack_pins("GPIO", {dio_to_pin(pin_to_dio(pin)): "GPIO"})
        except Exception:
            pass

    def _get_device(self):
        """Get the UD device object from the global handle manager."""
        from lager.io.labjack_ud_handle import get_ud_device
        return get_ud_device(self._model, self._serial)

    def _get_dio(self) -> int:
        """Convert the pin identifier to a UD DIO number.

        Raises:
            ValueError: on an unparseable or out-of-range pin.
        """
        from lager.io.labjack_ud_handle import pin_to_dio
        try:
            return pin_to_dio(self._pin)
        except ValueError as e:
            raise ValueError(f"{e} (net '{self._name}')") from None

    def _parse_level(self, level: int | str) -> int:
        """
        Parse level input to 0 or 1.

        Args:
            level: String or integer level specification.

        Returns:
            0 for LOW, 1 for HIGH.
        """
        if isinstance(level, str):
            level_str = level.strip().lower()
            return 1 if level_str in ("1", "on", "high", "true") else 0
        return 1 if int(level) else 0

    def _prepare(self):
        """Resolve the device and DIO, and force the line into digital mode.

        The mode step is the one that has no T7 equivalent and the one that
        fails quietly if skipped: ``BitStateRead`` documents that "only digital
        lines return valid readings", so a line left in analog mode returns a
        number rather than an error. ``set_channel_mode`` memoizes, so this
        costs a USB round trip once per pin.
        """
        from lager.io.labjack_ud_handle import set_channel_mode
        device = self._get_device()
        dio = self._get_dio()
        set_channel_mode(device, dio, analog=False)
        return device, dio

    def input(self) -> int:
        """
        Read the current state of the GPIO pin.

        Returns:
            0 for LOW, 1 for HIGH.

        Raises:
            ValueError: If the pin is not a usable digital line.
            RuntimeError: If LabJackPython or the Exodriver is unavailable.
            Exception: For device communication errors.
        """
        from lager.io.labjack_ud_handle import load_ud_module

        device, dio = self._prepare()
        ud = load_ud_module(self._model)

        # getFeedback returns one result per command; BitStateRead yields the
        # bit. Unlike the T7 this does not disturb the line's direction, so
        # there is nothing to save and restore.
        result = device.getFeedback(ud.BitStateRead(dio))
        value = 1 if int(result[0]) else 0
        _debug(f"Read DIO{dio} = {value} for net '{self._name}'")
        return value

    def output(self, level: int | str) -> None:
        """
        Set the output state of the GPIO pin.

        Args:
            level: Output level - accepts int (0/1) or str ("low"/"high",
                   "off"/"on", "0"/"1").

        Raises:
            ValueError: If the pin is not a usable digital line.
            RuntimeError: If LabJackPython or the Exodriver is unavailable.
            Exception: For device communication errors.
        """
        from lager.io.labjack_ud_handle import load_ud_module

        device, dio = self._prepare()
        ud = load_ud_module(self._model)
        value = self._parse_level(level)

        # BitStateWrite forces the line to output by itself -- no separate
        # BitDirWrite needed, and issuing one would only add a round trip.
        _debug(f"Writing {value} to DIO{dio} for net '{self._name}'")
        device.getFeedback(ud.BitStateWrite(dio, value))
