# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
LabJack U3 (UD series) SPI driver implementing the SPIBase interface.

Not a variant of the T7 driver -- a different stack end to end. The T7 reaches
SPI through named Modbus registers over LJM; LJM does not talk to a U3 at all.
A U3 has one low-level extended command, ``0xF8/0x3A``, reached through
LabJackPython over the Exodriver and surfaced as ``u3.U3.spi()``. It requires
U3 hardware version 1.21 or greater.

Things that differ from the T7 and are visible to a user:

- **50 bytes per transfer**, not the T7's 56. That is the command's own limit.
- **Clock is a divisor, not a frequency.** See ``_clock_factor_for``. The
  reachable set is coarse and has a gap just below the top.
- **CS is active-low only.** ``AutoCS`` drives CS low for the transfer and
  releases it at the end; there is no polarity bit and no hold bit, so
  ``cs_active="high"`` and ``keep_cs=True`` are refused rather than ignored.
- **The response comes back padded.** ``u3.spi()`` pads an odd-length transfer
  and does NOT trim the reply, so this driver trims. Its I2C sibling is the
  opposite: ``u3.i2c()`` trims its own odd response.
- **Pins must be digital.** ``DisableDirConfig=False`` makes the firmware set
  each line's DIRECTION, but nothing in the SPI command touches the
  analog/digital mux. That is owned by the handle manager; see ``_prepare``.
"""
from __future__ import annotations

import math
import os
import sys
from typing import List, Optional

from .spi_base import SPIBase
from lager.exceptions import SPIBackendError

DEBUG = bool(os.environ.get("LAGER_SPI_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_SPI_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"SPI_DEBUG: {msg}\n")
        sys.stderr.flush()


# Command 0xF8/0x3A's own limit. The T7's is 56, for a different device.
MAX_BYTES_PER_TRANSACTION = 50

# u3.spi() takes a mode LETTER. The datasheet's mapping is A=CPOL0/CPHA0,
# B=CPOL0/CPHA1, C=CPOL1/CPHA0, D=CPOL1/CPHA1, which is SPI modes 0-3 in
# order, so the letter is only a spelling of the same two-bit field.
_SPI_MODES = ("A", "B", "C", "D")

# SPIClockFactor, U3 datasheet section 5.2.15:
#   Frequency = 1000000 / (10 + 10 * (256 - SPIClockFactor))
# with the wire value 0 meaning a factor of 256, the maximum.
#
# The factor is an integer, so the reachable set is coarse and has a GAP at the
# top: factor 256 gives 100 kHz and factor 255 gives 50 kHz, with nothing in
# between. A request for 80 kHz therefore lands on 50 kHz. On top of that
# LabJack measures a real U3 ceiling near 80 kHz rather than the formula's
# 100 kHz, because the limit is the firmware's bit-banging rate and not the
# divisor. Both are why the achieved clock is reported as approximate.
MIN_CLOCK_FACTOR = 1
MAX_CLOCK_FACTOR = 256
SPI_MAX_FREQ_HZ = 100_000          # factor 256, the formula's ceiling
SPI_PRACTICAL_MAX_HZ = 80_000      # what LabJack measures on a real U3


class LabJackUDSPI(SPIBase):
    """SPI master on a LabJack U3's digital lines."""

    _speed_warning_shown = False

    def __init__(
        self,
        cs_pin=None,
        clk_pin=5,
        mosi_pin=7,
        miso_pin=6,
        mode: int = 0,
        bit_order: str = "msb",
        frequency_hz: Optional[int] = None,
        word_size: int = 8,
        cs_active: str = "low",
        cs_mode: str = "auto",
        unique_id: Optional[str] = None,
        model: str = "u3",
    ):
        """
        Initialize the U3 SPI driver.

        Defaults match LabJackPython's own (CS=FIO4, CLK=FIO5, MISO=FIO6,
        MOSI=FIO7), which is also what LabJack's U3 wiring diagrams show.

        Args:
            cs_pin: Chip select line; None when cs_mode is "manual"
            clk_pin: Clock line
            mosi_pin: MOSI line
            miso_pin: MISO line
            mode: SPI mode 0-3
            bit_order: "msb" or "lsb" ("lsb" is done in software)
            frequency_hz: Requested clock; None means as fast as the part goes
            word_size: Bits per word (8, 16, or 32)
            cs_active: "low" only -- see the module docstring
            cs_mode: "auto" (firmware drives CS) or "manual" (caller does)
            unique_id: The net's scanner address, used to open the right device
            model: UD model key, "u3"
        """
        from lager.io.labjack_ud_handle import serial_from_address

        self._cs_mode = str(cs_mode).lower()
        if self._cs_mode not in ("auto", "manual"):
            raise SPIBackendError(
                f"Invalid cs_mode '{cs_mode}'. Must be 'auto' or 'manual'.")

        self._cs_active = str(cs_active).lower()
        if self._cs_active != "low":
            raise SPIBackendError(
                f"cs_active='{cs_active}' is not supported on a LabJack U3. "
                f"The firmware's AutoCS drives CS low for the transfer and "
                f"releases it afterwards, and there is no polarity control. "
                f"For an active-high device use cs_mode='manual' and drive the "
                f"line yourself from a gpio net."
            )

        self._clk_dio = self._resolve_pin(clk_pin, "CLK")
        self._mosi_dio = self._resolve_pin(mosi_pin, "MOSI")
        self._miso_dio = self._resolve_pin(miso_pin, "MISO")
        self._cs_dio = (self._resolve_pin(cs_pin, "CS")
                        if cs_pin is not None else None)
        if self._cs_mode == "auto" and self._cs_dio is None:
            raise SPIBackendError(
                "cs_mode='auto' needs a cs_pin. Give one, or use "
                "cs_mode='manual' for a 3-wire net.")

        named = [d for d in (self._cs_dio, self._clk_dio, self._mosi_dio,
                             self._miso_dio) if d is not None]
        if len(set(named)) != len(named):
            raise SPIBackendError(
                "SPI pins must all be different lines; got "
                f"CS={self._cs_dio}, CLK={self._clk_dio}, "
                f"MOSI={self._mosi_dio}, MISO={self._miso_dio}."
            )

        self._set_mode(mode)
        self._set_bit_order(bit_order)
        self._set_word_size(word_size)
        self._clock_factor = self._clock_factor_for(frequency_hz)
        self._frequency_hz = frequency_hz

        self._serial = serial_from_address(unique_id)
        self._model = (model or "u3").lower()

        try:
            from lager.io.labjack_handle import register_labjack_pins
            pins = {
                self._pin_name(self._clk_dio): "CLK",
                self._pin_name(self._mosi_dio): "MOSI",
                self._pin_name(self._miso_dio): "MISO",
            }
            if self._cs_mode != "manual" and self._cs_dio is not None:
                pins[self._pin_name(self._cs_dio)] = "CS"
            register_labjack_pins("SPI", pins)
        except Exception:
            pass  # Advisory only; never break SPI over a bookkeeping failure.

    # -- validation helpers --

    def _set_mode(self, mode) -> None:
        if mode not in (0, 1, 2, 3):
            raise SPIBackendError(
                f"Invalid SPI mode {mode!r}. Must be 0, 1, 2 or 3.")
        self._mode = int(mode)

    def _set_bit_order(self, bit_order) -> None:
        value = str(bit_order).lower()
        if value not in ("msb", "lsb"):
            raise SPIBackendError(
                f"Invalid bit_order '{bit_order}'. Must be 'msb' or 'lsb'.")
        # LabJack firmware is MSB-first on every part; "lsb" is a software
        # reversal, shared with the T7 through SPIBase rather than reimplemented.
        self._bit_order = value

    def _set_word_size(self, word_size) -> None:
        if word_size not in (8, 16, 32):
            raise SPIBackendError(
                f"Invalid word_size {word_size!r}. Must be 8, 16 or 32.")
        self._word_size = int(word_size)

    @staticmethod
    def _pin_name(dio: int) -> str:
        from lager.io.labjack_ud_handle import dio_to_pin
        return dio_to_pin(dio)

    @staticmethod
    def _resolve_pin(pin, role: str) -> int:
        """Parse a pin and reject one the hardware cannot drive digitally.

        Rejected at construction rather than at the first transfer, and without
        opening the device: see the matching note in the U3 I2C driver.
        """
        from lager.io.labjack_ud_handle import pin_to_dio
        try:
            dio = pin_to_dio(pin)
        except ValueError as exc:
            raise SPIBackendError(f"Invalid {role} pin: {exc}") from None
        if dio <= 3:
            raise SPIBackendError(
                f"{role} pin FIO{dio} cannot be used for SPI. FIO0-FIO3 are "
                f"the U3-HV's fixed high-voltage analog inputs and no "
                f"configuration makes them digital. Use FIO4-FIO7, "
                f"EIO0-EIO7 or CIO0-CIO3 (EIO and CIO need the DB15)."
            )
        return dio

    # -- clock --

    @classmethod
    def _clock_factor_for(cls, frequency_hz) -> int:
        """Map a requested clock onto SPIClockFactor's effective 1..256.

        Rounds DOWN in frequency (floor on the factor), so the bus is never
        faster than asked. Warns once when the coarse divisor lands well below
        the request -- which the gap between factor 255 and 256 guarantees for
        anything between 50 kHz and 100 kHz.
        """
        if frequency_hz is None:
            return MAX_CLOCK_FACTOR          # as fast as the part goes
        try:
            freq = float(frequency_hz)
        except (TypeError, ValueError):
            raise SPIBackendError(
                f"Invalid frequency_hz: {frequency_hz!r}. Must be a number."
            ) from None
        if freq <= 0:
            raise SPIBackendError(
                f"Invalid frequency_hz: {frequency_hz!r}. Must be positive.")

        factor = math.floor(257 - (SPI_MAX_FREQ_HZ / freq))
        clamped = max(MIN_CLOCK_FACTOR, min(MAX_CLOCK_FACTOR, factor))
        achieved = cls._frequency_for(clamped)
        if achieved < 0.75 * freq and not cls._speed_warning_shown:
            cls._speed_warning_shown = True
            sys.stderr.write(
                f"WARNING: A LabJack U3 SPI clock is a coarse divisor. "
                f"{int(freq)} Hz was requested; the nearest reachable setting "
                f"at or below it is {achieved:.0f} Hz. The reachable range is "
                f"about {cls._frequency_for(MIN_CLOCK_FACTOR):.0f} Hz to "
                f"{SPI_PRACTICAL_MAX_HZ} Hz.\n"
            )
            sys.stderr.flush()
        return clamped

    @staticmethod
    def _frequency_for(clock_factor: int) -> float:
        """The approximate clock a given effective factor produces."""
        return 1e6 / (10 + 10 * (MAX_CLOCK_FACTOR - clock_factor))

    @property
    def _wire_clock_factor(self) -> int:
        """The byte that goes in the command; 256 is encoded as 0."""
        return 0 if self._clock_factor >= MAX_CLOCK_FACTOR else self._clock_factor

    @property
    def _max_words(self) -> int:
        return MAX_BYTES_PER_TRANSACTION // (self._word_size // 8)

    # -- device --

    def _get_device(self):
        from lager.io.labjack_ud_handle import get_ud_device
        return get_ud_device(self._model, self._serial)

    def _prepare(self):
        """Force every line this net drives digital, then hand back the device.

        Per transaction, not once: the handle manager memoizes on (serial, dio)
        so the steady-state cost is a dict lookup, and it is what undoes another
        net having claimed one of these lines as an analog input in between.

        Note this is NOT the same thing as ``DisableDirConfig=False``, which
        makes the firmware set each line's direction. Direction and the
        analog/digital mux are different registers, and setting one does
        nothing for the other.
        """
        from lager.io.labjack_ud_handle import set_channel_mode
        device = self._get_device()
        lines = [(self._clk_dio, "CLK"), (self._mosi_dio, "MOSI"),
                 (self._miso_dio, "MISO")]
        if self._cs_mode == "auto" and self._cs_dio is not None:
            lines.append((self._cs_dio, "CS"))
        for dio, role in lines:
            try:
                set_channel_mode(device, dio, analog=False)
            except ValueError as exc:
                raise SPIBackendError(f"{role}: {exc}") from None
        return device

    # -- transfers --

    def _transfer_bytes(self, tx: List[int]) -> List[int]:
        """One u3.spi() call. Returns exactly len(tx) bytes."""
        if len(tx) > MAX_BYTES_PER_TRANSACTION:
            raise SPIBackendError(
                f"Transfer of {len(tx)} bytes exceeds the U3's maximum of "
                f"{MAX_BYTES_PER_TRANSACTION}. Use at most {self._max_words} "
                f"words with a {self._word_size}-bit word size, or split into "
                f"multiple transfers."
            )
        if not tx:
            return []

        device = self._prepare()
        # CSPinNum is still a byte in the command when AutoCS is false, and the
        # firmware ignores it. Pass CLK so the field is a real digital line
        # rather than a number that means nothing.
        auto_cs = self._cs_mode == "auto"
        cs_num = self._cs_dio if (auto_cs and self._cs_dio is not None) \
            else self._clk_dio
        try:
            result = device.spi(
                list(tx),
                AutoCS=auto_cs,
                DisableDirConfig=False,
                SPIMode=_SPI_MODES[self._mode],
                SPIClockFactor=self._wire_clock_factor,
                CSPinNum=cs_num,
                CLKPinNum=self._clk_dio,
                MISOPinNum=self._miso_dio,
                MOSIPinNum=self._mosi_dio,
            )
        except SPIBackendError:
            raise
        except Exception as exc:
            raise SPIBackendError(f"U3 SPI transfer failed: {exc}") from exc

        received = [int(b) & 0xFF for b in result.get("SPIBytes", [])]
        # u3.spi() pads an odd-length transfer with one byte and returns the
        # reply at the PADDED length without trimming it. Hand that back
        # unchanged and every odd transfer grows a phantom trailing byte.
        # (u3.i2c() does trim its own odd response. They differ.)
        return received[:len(tx)]

    def config(
        self,
        mode: int = None,
        bit_order: str = None,
        frequency_hz: int = None,
        word_size: int = None,
        cs_active: str = None,
        cs_mode: str = None,
    ) -> None:
        """
        Configure SPI parameters. Only what is passed is changed.

        Args:
            mode: SPI mode 0-3, mapped onto the firmware's A-D
            bit_order: "msb", or "lsb" which is reversed in software
            frequency_hz: Requested clock. APPROXIMATE: the U3 takes a coarse
                          integer divisor, and LabJack measures a real ceiling
                          near 80 kHz rather than the formula's 100 kHz,
                          because the limit is the firmware's bit-banging rate.
                          Treat any configured value as an upper bound and put
                          a scope on the clock if it matters.
            word_size: 8, 16 or 32. Words above 8 bits are split in software.
            cs_active: "low" only. See the class docstring.
            cs_mode: "auto" or "manual"
        """
        if cs_mode is not None:
            value = str(cs_mode).lower()
            if value not in ("auto", "manual"):
                raise SPIBackendError(
                    f"Invalid cs_mode '{cs_mode}'. Must be 'auto' or 'manual'.")
            if value == "auto" and self._cs_dio is None:
                raise SPIBackendError(
                    "cs_mode='auto' needs a cs_pin, and this net has none.")
            self._cs_mode = value
        if cs_active is not None and str(cs_active).lower() != "low":
            raise SPIBackendError(
                f"cs_active='{cs_active}' is not supported on a LabJack U3. "
                f"AutoCS is active-low with no polarity control. Use "
                f"cs_mode='manual' and drive the line from a gpio net."
            )
        if mode is not None:
            self._set_mode(mode)
        if bit_order is not None:
            self._set_bit_order(bit_order)
        if word_size is not None:
            self._set_word_size(word_size)
        if frequency_hz is not None:
            self._clock_factor = self._clock_factor_for(frequency_hz)
            self._frequency_hz = frequency_hz
        _debug(f"config: mode={self._mode} bit_order={self._bit_order} "
               f"word_size={self._word_size} cs_mode={self._cs_mode} "
               f"factor={self._clock_factor} "
               f"(~{self._frequency_for(self._clock_factor):.0f}Hz)")

    def _check_keep_cs(self, keep_cs: bool) -> None:
        """keep_cs cannot be honoured while the firmware owns CS.

        Refused rather than ignored: the whole point of keep_cs is holding CS
        across two transfers, and a transfer that silently released it would
        succeed while returning data the device never meant to send.
        """
        if keep_cs and self._cs_mode == "auto":
            raise SPIBackendError(
                "keep_cs=True is not supported with cs_mode='auto' on a "
                "LabJack U3: the firmware releases CS at the end of every "
                "transfer and has no hold bit. Use cs_mode='manual' and drive "
                "CS from a gpio net, which holds it across as many transfers "
                "as you like."
            )
        # Under cs_mode='manual' the driver never touches CS, so it is already
        # held by whoever asserted it and keep_cs is satisfied by doing nothing.

    def read(
        self,
        n_words: int,
        fill: int = 0xFF,
        keep_cs: bool = False,
    ) -> List[int]:
        """Clock out *fill* and return what came back."""
        self._check_keep_cs(keep_cs)
        if n_words < 0:
            raise SPIBackendError(f"n_words must not be negative: {n_words}")
        if n_words > self._max_words:
            raise SPIBackendError(
                f"Cannot read {n_words} words: a U3 transfer is at most "
                f"{MAX_BYTES_PER_TRANSACTION} bytes, so at most "
                f"{self._max_words} words with a {self._word_size}-bit word "
                f"size."
            )
        return self.read_write([fill] * n_words, keep_cs=keep_cs)

    def read_write(
        self,
        data: List[int],
        keep_cs: bool = False,
    ) -> List[int]:
        """Full-duplex transfer: send *data*, return the same number of words."""
        self._check_keep_cs(keep_cs)
        tx = self.words_to_bytes(list(data), word_size=self._word_size,
                                 bit_order=self._bit_order)
        received = self._transfer_bytes(tx)
        return self.bytes_to_words(received, word_size=self._word_size,
                                   bit_order=self._bit_order)
