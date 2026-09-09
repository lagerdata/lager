# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
LabJack U3 (UD series) I2C driver implementing the I2CBase interface.

Not a variant of the T7 driver -- a different stack end to end. The T7 reaches
I2C through named Modbus registers over LJM; LJM does not talk to a U3 at all.
A U3 has one low-level extended command, ``0xF8/0x3B``, reached through
LabJackPython over the Exodriver and surfaced as ``u3.U3.i2c()``. It requires
U3 hardware version 1.21 or greater.

Consequences worth knowing before reading the code:

- **One USB round trip per transaction.** The protocol is bit-banged in
  firmware, so there is no batching and no streaming. Master only.
- **50 bytes TX, 52 bytes RX.** These are the command's own limits and are NOT
  the T7's 56. Do not unify them.
- **Speed is a delay count, not a frequency.** See ``_speed_adjust_for``.
- **There are no internal pull-ups.** SDA and SCL both need external resistors
  to VS; LabJack recommends 4.7k. Without them every address NAKs and a scan
  comes back empty, which looks exactly like an empty bus.
- **Pins must be digital.** On a U3 a flexible line is analog or digital
  depending on a whole-device bitmask, and the I2C command does not touch it.
  That is owned by the handle manager; see ``_prepare``.
"""
from __future__ import annotations

import math
import os
import sys
from typing import List, Optional

from .i2c_base import I2CBase
from lager.exceptions import I2CBackendError

DEBUG = bool(os.environ.get("LAGER_I2C_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_I2C_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"I2C_DEBUG: {msg}\n")
        sys.stderr.flush()


# Command 0xF8/0x3B's own limits, enforced as exceptions inside u3.i2c().
# The T7's MAX_BYTES_PER_TRANSACTION is 56 for both directions; these are
# different numbers for a different device and must never be unified with it.
MAX_TX_BYTES = 50
MAX_RX_BYTES = 52

# AckArray is 32 bits wide but the TX limit is 50 bytes. The address byte's
# ACK sits at bit index len(data), so once a transfer carries 31 or more data
# bytes the address bit falls off the top and cannot be observed at all.
MAX_OBSERVABLE_ACK_BYTES = 31

# SpeedAdjust anchors, U3 datasheet section 5.2.19. LabJack publishes these
# three points, no formula, and calls the relationship non-linear.
#
# The FREQUENCY is non-linear; the PERIOD is not. Fitting a line to the period
# through the two extreme anchors and then predicting the third -- which is not
# an input to the fit -- gives 6.667 + 20*0.36601 = 13.99 us = 71.5 kHz against
# a documented ~70 kHz, a 2% error. That is a real cross-check, so this ships
# an affine period model rather than a three-entry lookup table.
I2C_MAX_FREQ_HZ = 150_000          # SpeedAdjust 0
I2C_MIN_FREQ_HZ = 10_000           # SpeedAdjust 255
_PERIOD_US_AT_ZERO = 1e6 / I2C_MAX_FREQ_HZ           # 6.667 us
_PERIOD_US_AT_MAX = 1e6 / I2C_MIN_FREQ_HZ            # 100.0 us
_PERIOD_US_PER_COUNT = (_PERIOD_US_AT_MAX - _PERIOD_US_AT_ZERO) / 255
MAX_SPEED_ADJUST = 255


class LabJackUDI2C(I2CBase):
    """I2C master on a LabJack U3's digital lines."""

    _speed_warning_shown = False

    def __init__(
        self,
        sda_pin,
        scl_pin,
        frequency_hz: int = 100_000,
        unique_id: Optional[str] = None,
        model: str = "u3",
    ):
        """
        Initialize the U3 I2C driver.

        Args:
            sda_pin: SDA line, as a DIO number or a name such as "FIO6"
            scl_pin: SCL line, as a DIO number or a name such as "FIO7"
            frequency_hz: Requested bus clock. Approximate; see config().
            unique_id: The net's scanner address, used to open the right device
                       by serial. A U3 reports no USB serial, so this is
                       routinely empty, which resolves to "first found".
            model: UD model key, "u3"
        """
        from lager.io.labjack_ud_handle import serial_from_address

        self._sda_dio = self._resolve_pin(sda_pin, "SDA")
        self._scl_dio = self._resolve_pin(scl_pin, "SCL")
        if self._sda_dio == self._scl_dio:
            raise I2CBackendError(
                f"SDA and SCL are both {self._pin_name(self._sda_dio)}. "
                f"They must be different lines."
            )

        self._frequency_hz = frequency_hz
        self._speed_adjust = self._speed_adjust_for(frequency_hz)
        self._serial = serial_from_address(unique_id)
        self._model = (model or "u3").lower()

        _debug(f"LabJackUDI2C initialized: SDA={self._pin_name(self._sda_dio)}, "
               f"SCL={self._pin_name(self._scl_dio)}, freq={frequency_hz}Hz, "
               f"SpeedAdjust={self._speed_adjust}")

        try:
            from lager.io.labjack_handle import register_labjack_pins
            register_labjack_pins("I2C", {
                self._pin_name(self._sda_dio): "SDA",
                self._pin_name(self._scl_dio): "SCL",
            })
        except Exception:
            pass  # Advisory only; never break I2C over a bookkeeping failure.

    # -- pins --

    @staticmethod
    def _pin_name(dio: int) -> str:
        from lager.io.labjack_ud_handle import dio_to_pin
        return dio_to_pin(dio)

    @staticmethod
    def _resolve_pin(pin, role: str) -> int:
        """Parse a pin and reject one the hardware cannot drive digitally.

        Rejected here, at construction, rather than at the first transaction.
        A net whose pins can never work should fail when it is configured, not
        halfway through a run on the bench.

        FIO0-FIO3 are the U3-HV's fixed high-voltage analog inputs; no mask bit
        makes them digital. This does not open the device to check ``isHV``:
        constructing a driver must not claim USB, the scanner already treats
        every U3 as an HV for exactly this reason, and of the two ways to be
        wrong, accepting a pin that cannot work is the worse one.
        """
        from lager.io.labjack_ud_handle import pin_to_dio
        try:
            dio = pin_to_dio(pin)
        except ValueError as exc:
            raise I2CBackendError(f"Invalid {role} pin: {exc}") from None
        if dio <= 3:
            raise I2CBackendError(
                f"{role} pin FIO{dio} cannot be used for I2C. FIO0-FIO3 are "
                f"the U3-HV's fixed high-voltage analog inputs and no "
                f"configuration makes them digital. Use FIO4-FIO7, "
                f"EIO0-EIO7 or CIO0-CIO3 (EIO and CIO need the DB15)."
            )
        return dio

    # -- speed --

    @classmethod
    def _speed_adjust_for(cls, frequency_hz) -> int:
        """Map a requested bus clock onto the SpeedAdjust delay count.

        The achieved clock is approximate and, by construction, no faster than
        requested: the count is rounded up, which rounds the frequency down.
        Out-of-range requests clamp with a one-shot warning rather than raise,
        matching the T7 drivers -- a frequency is a request for a bit rate, not
        a correctness contract.
        """
        try:
            freq = float(frequency_hz)
        except (TypeError, ValueError):
            raise I2CBackendError(
                f"Invalid frequency_hz: {frequency_hz!r}. Must be a number."
            ) from None
        if freq <= 0:
            raise I2CBackendError(
                f"Invalid frequency_hz: {frequency_hz!r}. Must be positive."
            )

        adjust = math.ceil(
            (1e6 / freq - _PERIOD_US_AT_ZERO) / _PERIOD_US_PER_COUNT)
        clamped = max(0, min(MAX_SPEED_ADJUST, adjust))
        if clamped != adjust and not cls._speed_warning_shown:
            cls._speed_warning_shown = True
            sys.stderr.write(
                f"WARNING: A LabJack U3 I2C bus runs between "
                f"{I2C_MIN_FREQ_HZ} Hz and {I2C_MAX_FREQ_HZ} Hz. "
                f"{int(freq)} Hz was requested; using "
                f"{cls._frequency_for(clamped):.0f} Hz.\n"
            )
            sys.stderr.flush()
        return clamped

    @staticmethod
    def _frequency_for(speed_adjust: int) -> float:
        """The approximate clock a given SpeedAdjust produces."""
        return 1e6 / (_PERIOD_US_AT_ZERO
                      + speed_adjust * _PERIOD_US_PER_COUNT)

    # -- device --

    def _get_device(self):
        from lager.io.labjack_ud_handle import get_ud_device
        return get_ud_device(self._model, self._serial)

    def _prepare(self):
        """Force both lines digital, then hand back the device.

        Done on every transaction rather than once. The handle manager memoizes
        on (serial, dio), so the steady-state cost is a dict lookup, and it is
        the only thing that survives another net flipping the pin: an adc net
        on AIN6 and this bus's SDA on FIO6 are the same physical line, and the
        adc read sets that line analog.

        The mask is never written from here. It is whole-device state shared
        across every net and role, so the read-modify-write lives under the
        manager's lock.
        """
        from lager.io.labjack_ud_handle import set_channel_mode
        device = self._get_device()
        for dio, role in ((self._sda_dio, "SDA"), (self._scl_dio, "SCL")):
            try:
                set_channel_mode(device, dio, analog=False)
            except ValueError as exc:
                raise I2CBackendError(f"{role}: {exc}") from None
        return device

    # -- ACK decoding --

    @staticmethod
    def _ack_value(ack_array) -> int:
        """Combine u3.i2c()'s four AckArray bytes into one 32-bit value."""
        value = 0
        for index, byte in enumerate(list(ack_array)[:4]):
            value |= (int(byte) & 0xFF) << (8 * index)
        return value

    @staticmethod
    def _address_acked(value: int, n_tx: int) -> bool:
        """Whether the slave acknowledged its address.

        AckArray sets a bit per acknowledged WRITE byte, but numbered from the
        end: bit 0 is the LAST data byte and the address byte is the highest,
        at bit index n_tx. So the address bit moves with the transfer length --
        it is bit 0 only when there are no data bytes, which is the scan case.
        """
        if n_tx >= 32:
            return True  # unobservable; see MAX_OBSERVABLE_ACK_BYTES
        return bool((value >> n_tx) & 1)

    def _check_acks(self, value: int, address: int, n_tx: int) -> None:
        """Raise unless the address and every transmitted byte were ACKed."""
        if n_tx >= MAX_OBSERVABLE_ACK_BYTES:
            # Above 31 data bytes the address ACK has shifted out of the
            # 32-bit field, so only a fully-set word can be verified.
            if value != 0xFFFFFFFF:
                raise I2CBackendError(
                    f"No ACK from device at 0x{address:02x} during a "
                    f"{n_tx}-byte write (AckArray 0x{value:08x}; a transfer "
                    f"this long cannot report which byte failed)."
                )
            return

        if not self._address_acked(value, n_tx):
            raise I2CBackendError(
                f"No ACK from device at 0x{address:02x}. Check the address, "
                f"that the device is powered, and that SDA and SCL have "
                f"external pull-up resistors -- a U3 has none."
            )

        expected = (1 << (n_tx + 1)) - 1
        if value != expected:
            # The address ACKed, so something is there; find the first byte it
            # refused. Data byte i sits at bit index n_tx - 1 - i.
            for i in range(n_tx):
                if not (value >> (n_tx - 1 - i)) & 1:
                    raise I2CBackendError(
                        f"Device at 0x{address:02x} acknowledged its address "
                        f"but NAKed data byte {i} of {n_tx} "
                        f"(AckArray 0x{value:08x})."
                    )
            raise I2CBackendError(
                f"Unexpected AckArray 0x{value:08x} from 0x{address:02x} "
                f"(expected 0x{expected:08x})."
            )

    # -- transactions --

    def _transact(self, device, address: int, tx: List[int], num_rx: int,
                  reset: bool = False) -> dict:
        """One u3.i2c() call, with our own limit checks and error wrapping."""
        if len(tx) > MAX_TX_BYTES:
            raise I2CBackendError(
                f"TX size {len(tx)} exceeds the U3's maximum of "
                f"{MAX_TX_BYTES} bytes."
            )
        if num_rx > MAX_RX_BYTES:
            raise I2CBackendError(
                f"RX size {num_rx} exceeds the U3's maximum of "
                f"{MAX_RX_BYTES} bytes."
            )
        try:
            # Address is passed UNSHIFTED: u3.i2c() does the << 1 itself.
            return device.i2c(
                address,
                list(tx),
                SpeedAdjust=self._speed_adjust,
                SDAPinNum=self._sda_dio,
                SCLPinNum=self._scl_dio,
                NumI2CBytesToReceive=num_rx,
                ResetAtStart=reset,
            )
        except I2CBackendError:
            raise
        except Exception as exc:
            raise I2CBackendError(
                f"U3 I2C transaction failed at 0x{address:02x}: {exc}"
            ) from exc

    def config(
        self,
        frequency_hz: int = 100_000,
        pull_ups: Optional[bool] = None,
    ) -> None:
        """
        Configure I2C bus parameters.

        Args:
            frequency_hz: Requested bus clock. The U3 takes a SpeedAdjust delay
                          count rather than a frequency, so the achieved clock
                          is APPROXIMATE and is rounded down, never up. The
                          reachable range is about 10 kHz to 150 kHz; a request
                          outside it clamps with a warning.
            pull_ups: Accepted and ignored, as on the T7. A U3 has no internal
                      pull-ups at all -- SDA and SCL need external resistors to
                      VS, 4.7k being the usual choice.
        """
        self._frequency_hz = frequency_hz
        self._speed_adjust = self._speed_adjust_for(frequency_hz)
        if pull_ups is not None:
            _debug("pull_ups is not settable on a U3; the part has none. "
                   "Fit external resistors on SDA and SCL.")
        _debug(f"config: freq={frequency_hz}Hz -> SpeedAdjust="
               f"{self._speed_adjust} (~{self._frequency_for(self._speed_adjust):.0f}Hz)")

    def scan(
        self,
        start_addr: int = 0x08,
        end_addr: int = 0x77,
    ) -> List[int]:
        """
        Scan the bus for devices that acknowledge their address.

        Each probe is an address-only transaction: zero bytes written, zero
        read. Nothing is sent to the device beyond its address, so this cannot
        disturb a register the way a dummy one-byte write would.

        With no pull-ups fitted every address NAKs and this returns an empty
        list -- indistinguishable from an empty bus.
        """
        self._validate_address(start_addr)
        self._validate_address(end_addr)
        if start_addr > end_addr:
            raise I2CBackendError(
                f"start_addr 0x{start_addr:02x} is above end_addr "
                f"0x{end_addr:02x}."
            )

        device = self._prepare()   # hoisted: once per scan, not once per probe
        found = []
        for address in range(start_addr, end_addr + 1):
            try:
                result = self._transact(device, address, [], 0,
                                        reset=(address == start_addr))
            except I2CBackendError:
                continue  # a probe that errors is simply not a device
            if self._address_acked(self._ack_value(result.get("AckArray", [])), 0):
                found.append(address)
        _debug(f"scan 0x{start_addr:02x}-0x{end_addr:02x} found "
               f"{[hex(a) for a in found]}")
        return found

    def read(
        self,
        address: int,
        num_bytes: int,
    ) -> List[int]:
        """Read *num_bytes* from *address*."""
        self._validate_address(address)
        if num_bytes < 0:
            raise I2CBackendError(f"num_bytes must not be negative: {num_bytes}")
        device = self._prepare()
        result = self._transact(device, address, [], num_bytes)
        self._check_acks(self._ack_value(result.get("AckArray", [])), address, 0)
        # u3.i2c() trims its own odd-length response, so this is already
        # exactly num_bytes long. Unlike u3.spi(), which does not.
        return [int(b) & 0xFF for b in result.get("I2CBytes", [])]

    def write(
        self,
        address: int,
        data: List[int],
    ) -> None:
        """Write *data* to *address*."""
        self._validate_address(address)
        payload = [int(b) & 0xFF for b in data]
        device = self._prepare()
        result = self._transact(device, address, payload, 0)
        self._check_acks(self._ack_value(result.get("AckArray", [])),
                         address, len(payload))

    def write_read(
        self,
        address: int,
        data: List[int],
        num_bytes: int,
    ) -> List[int]:
        """Write then read in one transaction (repeated start)."""
        self._validate_address(address)
        if num_bytes < 0:
            raise I2CBackendError(f"num_bytes must not be negative: {num_bytes}")
        payload = [int(b) & 0xFF for b in data]
        device = self._prepare()
        result = self._transact(device, address, payload, num_bytes)
        self._check_acks(self._ack_value(result.get("AckArray", [])),
                         address, len(payload))
        return [int(b) & 0xFF for b in result.get("I2CBytes", [])]
