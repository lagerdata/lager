# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
LabJack T7 I2C driver implementing the I2CBase interface.

Provides I2C communication via LabJack T7 digital I/O pins using
the built-in I2C functionality.

LabJack T7 I2C Registers:
- I2C_SDA_DIONUM (5100): SDA pin number (FIO/EIO/CIO/MIO number)
- I2C_SCL_DIONUM (5101): SCL pin number
- I2C_SPEED_THROTTLE (5102): Clock speed control. Counts DOWN from 65536:
                       0 is equivalent to 65536 (max, ~450kHz), 65516 is
                       ~100kHz. Firmware rejects values below 46000 (~130Hz)
                       with error 2729 (I2C_SPEED_TOO_LOW).
- I2C_OPTIONS (5103): Bit 0 = reset bus, Bit 1 = no-stop (repeated start),
                       Bit 2 = enable clock stretching
- I2C_SLAVE_ADDRESS (5104): Un-shifted 7-bit I2C slave address
- I2C_NUM_BYTES_TX (5108): Number of bytes to transmit
- I2C_NUM_BYTES_RX (5109): Number of bytes to receive
- I2C_DATA_TX (5120): Transmit buffer
- I2C_DATA_RX (5160): Receive buffer
- I2C_GO (5110): Write 1 to execute transaction
- I2C_ACKS (5114): ACK results

Constraints:
- Maximum 56 bytes per transaction (hardware buffer limit)
"""
from __future__ import annotations

import os
import sys
import time
from typing import List, Optional

from .i2c_base import I2CBase
from lager.exceptions import I2CBackendError

DEBUG = bool(os.environ.get("LAGER_I2C_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_I2C_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"I2C_DEBUG: {msg}\n")
        sys.stderr.flush()


# Maximum bytes per LabJack I2C transaction.
# The T7 I2C data buffers are limited to 56 bytes each.
MAX_BYTES_PER_TRANSACTION = 56

# I2C_SPEED_THROTTLE semantics (T-series datasheet, section 13.3):
# the register counts down from 65536. Writing 0 is equivalent to 65536
# (max speed, ~450 kHz); smaller values slow the clock (65516 ~= 100 kbps).
# The firmware rejects values below 46000 with error 2729
# (I2C_SPEED_TOO_LOW: "throttle setting is too low, watchdog may fire").
THROTTLE_MAX_FREQ_HZ = 450_000
THROTTLE_FLOOR = 46_000  # slowest allowed, ~130 Hz
# Added clock period per throttle count below 65536, derived from the
# datasheet anchor 65516 ~= 100 kHz. T-series I2C is bit-banged in
# firmware, so achieved bus speed is approximate and varies with load.
THROTTLE_SECONDS_PER_COUNT = 3.889e-7


class LabJackI2C(I2CBase):
    """
    LabJack T7 I2C implementation.

    Uses the LabJack T7's built-in I2C functionality to communicate
    with I2C devices.

    Pin configuration is provided via the net configuration 'params':
    - sda_pin: SDA pin number (e.g., 4 for FIO4)
    - scl_pin: SCL pin number (e.g., 5 for FIO5)
    """

    # Class-level flags: print each throttle warning only once per session
    _throttle_warning_shown = False
    _speed_fallback_warning_shown = False

    def __init__(
        self,
        sda_pin: int,
        scl_pin: int,
        frequency_hz: int = 100_000,
    ):
        self._sda_pin = int(sda_pin)
        self._scl_pin = int(scl_pin)
        self._frequency_hz = frequency_hz
        # Set when firmware rejects a computed throttle (error 2729);
        # forces max speed until the next config() call.
        self._force_max_speed = False

        _debug(f"LabJackI2C initialized: SDA={sda_pin}, SCL={scl_pin}, "
               f"freq={frequency_hz}Hz")

        # Register pins with the conflict tracker so overlapping usage
        # within a single lager-python script produces a warning.
        try:
            from lager.io.labjack_handle import register_labjack_pins, PinRegistry
            register_labjack_pins("I2C", {
                PinRegistry.dio_to_name(self._sda_pin): "SDA",
                PinRegistry.dio_to_name(self._scl_pin): "SCL",
            })
        except Exception:
            pass

    def _get_handle(self) -> int:
        """Get LabJack handle from the global handle manager."""
        from lager.io.labjack_handle import get_labjack_handle
        return get_labjack_handle()

    def _get_ljm(self):
        """Get the ljm module."""
        from lager.io.labjack_handle import ljm, _LJM_ERR
        if ljm is None:
            raise I2CBackendError(f"LabJack LJM library not available: {_LJM_ERR}")
        return ljm

    def _frequency_to_throttle(self, frequency_hz: int) -> int:
        """
        Convert frequency in Hz to LabJack I2C_SPEED_THROTTLE value.

        The throttle counts down from 65536: writing 0 is equivalent to
        65536 (max speed, ~450 kHz) and smaller values slow the clock
        (65516 ~= 100 kHz per the T-series datasheet). The firmware
        rejects values below 46000 (~130 Hz) with error 2729.
        """
        if frequency_hz <= 0:
            raise I2CBackendError(
                f"Invalid I2C frequency: {frequency_hz}Hz. "
                f"Frequency must be a positive integer."
            )

        if self._force_max_speed:
            return 0

        if frequency_hz >= THROTTLE_MAX_FREQ_HZ:
            return 0

        extra_period = 1.0 / frequency_hz - 1.0 / THROTTLE_MAX_FREQ_HZ
        throttle = 65536 - round(extra_period / THROTTLE_SECONDS_PER_COUNT)

        if throttle >= 65536:
            return 0

        if throttle < THROTTLE_FLOOR:
            if not LabJackI2C._throttle_warning_shown:
                sys.stderr.write(
                    f"WARNING: LabJack I2C minimum clock is ~130Hz; clamping "
                    f"requested {frequency_hz}Hz to firmware floor "
                    f"(throttle {THROTTLE_FLOOR})\n"
                )
                sys.stderr.flush()
                LabJackI2C._throttle_warning_shown = True
            return THROTTLE_FLOOR

        return throttle

    def _setup_i2c_registers(self, handle: int, ljm, address: int,
                              num_bytes_tx: int, num_bytes_rx: int,
                              options: int = 0x04) -> None:
        """
        Configure LabJack I2C registers for a transaction.

        Args:
            handle: LabJack device handle
            ljm: LabJack module
            address: 7-bit I2C device address
            num_bytes_tx: Number of bytes to transmit
            num_bytes_rx: Number of bytes to receive
            options: I2C_OPTIONS register value (default: clock stretching enabled)
        """
        # Configure I2C pins
        ljm.eWriteName(handle, "I2C_SDA_DIONUM", self._sda_pin)
        ljm.eWriteName(handle, "I2C_SCL_DIONUM", self._scl_pin)

        # Configure speed throttle
        throttle = self._frequency_to_throttle(self._frequency_hz)
        ljm.eWriteName(handle, "I2C_SPEED_THROTTLE", throttle)

        # Configure options (enable clock stretching by default)
        ljm.eWriteName(handle, "I2C_OPTIONS", options)

        # Set slave address (un-shifted 7-bit address)
        # The LJM I2C_SLAVE_ADDRESS register takes the raw 7-bit address;
        # the T7 firmware handles the R/W bit internally.
        ljm.eWriteName(handle, "I2C_SLAVE_ADDRESS", address)

        # Set byte counts
        ljm.eWriteName(handle, "I2C_NUM_BYTES_TX", num_bytes_tx)
        ljm.eWriteName(handle, "I2C_NUM_BYTES_RX", num_bytes_rx)

        _debug(f"I2C registers configured: addr=0x{address:02x}, "
               f"tx={num_bytes_tx}, rx={num_bytes_rx}, "
               f"throttle={throttle}, options=0x{options:02x}")

    def _execute_transaction(
        self,
        address: int,
        tx_data: List[int],
        num_rx: int,
        options: int = 0x04,
        max_retries: int = 5,
    ) -> List[int]:
        """
        Execute a single I2C transaction with retry logic.

        Args:
            address: 7-bit I2C device address
            tx_data: Bytes to transmit (can be empty for read-only)
            num_rx: Number of bytes to receive (can be 0 for write-only)
            options: I2C_OPTIONS register value
            max_retries: Maximum number of retry attempts for reconnect errors

        Returns:
            List of received bytes (empty if num_rx is 0)
        """
        self._validate_address(address)
        num_tx = len(tx_data)
        if num_tx > MAX_BYTES_PER_TRANSACTION:
            raise I2CBackendError(
                f"TX size {num_tx} exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes."
            )
        if num_rx > MAX_BYTES_PER_TRANSACTION:
            raise I2CBackendError(
                f"RX size {num_rx} exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes."
            )

        last_error = None

        for attempt in range(max_retries):
            try:
                handle = self._get_handle()
                ljm = self._get_ljm()

                # Configure I2C registers
                self._setup_i2c_registers(
                    handle, ljm, address, num_tx, num_rx, options
                )

                # Write TX data if any
                if num_tx > 0:
                    ljm.eWriteNameByteArray(
                        handle, "I2C_DATA_TX", num_tx, tx_data
                    )
                    _debug(f"TX bytes: {[hex(b) for b in tx_data]}")

                # Execute transaction
                ljm.eWriteName(handle, "I2C_GO", 1)

                # Verify the device acknowledged. I2C_ACKS reports the
                # number of ACKs received during the transaction. If 0,
                # the slave did not ACK the address byte (no device at
                # this address, or bus not functioning).
                acks = int(ljm.eReadName(handle, "I2C_ACKS"))
                if acks == 0 and (num_tx > 0 or num_rx > 0):
                    raise I2CBackendError(
                        f"No ACK from device at 0x{address:02x}. "
                        f"Check wiring and address."
                    )
                _debug(f"I2C_ACKS={acks}")

                # Read RX data if any
                rx_bytes = []
                if num_rx > 0:
                    rx_bytes = list(
                        ljm.eReadNameByteArray(handle, "I2C_DATA_RX", num_rx)
                    )
                    _debug(f"RX bytes: {[hex(b) for b in rx_bytes]}")

                return rx_bytes

            except Exception as e:
                last_error = e
                error_str = str(e)

                # Firmware rejected the computed throttle (error 2729,
                # I2C_SPEED_TOO_LOW). Should not happen with the
                # THROTTLE_FLOOR clamp, but if a firmware rev enforces a
                # higher floor, degrade to max speed instead of failing.
                is_speed_rejected = (
                    "2729" in error_str or
                    "SPEED_TOO_LOW" in error_str.upper()
                )
                if is_speed_rejected and not self._force_max_speed:
                    self._force_max_speed = True
                    if not LabJackI2C._speed_fallback_warning_shown:
                        sys.stderr.write(
                            f"WARNING: LabJack firmware rejected I2C throttle "
                            f"for {self._frequency_hz}Hz (error 2729); falling "
                            f"back to max speed ~450kHz\n"
                        )
                        sys.stderr.flush()
                        LabJackI2C._speed_fallback_warning_shown = True
                    continue

                is_recoverable = (
                    "1227" in error_str or
                    "1239" in error_str or
                    "RECONNECT" in error_str.upper() or
                    "NOT_FOUND" in error_str.upper()
                )
                if is_recoverable:
                    _debug(f"Reconnect error on attempt "
                           f"{attempt + 1}/{max_retries}: {e}")
                    if attempt < max_retries - 1:
                        wait_time = 2.0 * (2 ** attempt)
                        _debug(f"Waiting {wait_time}s before retry...")
                        time.sleep(wait_time)
                        continue
                    else:
                        raise I2CBackendError(
                            f"LabJack device error after {max_retries} attempts. "
                            f"Ensure the device is connected and try again. "
                            f"Error: {e}"
                        )
                else:
                    raise I2CBackendError(f"I2C transaction failed: {e}")

        raise I2CBackendError(f"I2C transaction failed: {last_error}")

    def config(
        self,
        frequency_hz: int = 100_000,
        pull_ups: Optional[bool] = None,
    ) -> None:
        """
        Configure I2C bus parameters.

        Args:
            frequency_hz: I2C clock frequency in Hz
            pull_ups: Ignored for LabJack (no internal pull-ups)
        """
        self._frequency_hz = frequency_hz
        # Re-arm the computed throttle path after a firmware fallback.
        self._force_max_speed = False

        if pull_ups is not None:
            _debug("LabJack does not have internal I2C pull-ups, "
                   "pull_ups parameter ignored")

        _debug(f"I2C configured: freq={frequency_hz}Hz")

    def scan(
        self,
        start_addr: int = 0x08,
        end_addr: int = 0x77,
    ) -> List[int]:
        """
        Scan I2C bus for connected devices.

        Attempts a 0-byte write to each address. Devices that ACK
        are reported as found.

        Args:
            start_addr: First 7-bit address to probe
            end_addr: Last 7-bit address to probe

        Returns:
            List of 7-bit addresses that responded with ACK
        """
        self._validate_address(start_addr)
        self._validate_address(end_addr)
        found = []
        handle = self._get_handle()
        ljm = self._get_ljm()

        # Use throttle=0 (max speed) for scanning - probing for ACKs is
        # timing-insensitive and max speed keeps the address sweep fast
        for addr in range(start_addr, end_addr + 1):
            try:
                ljm.eWriteName(handle, "I2C_SDA_DIONUM", self._sda_pin)
                ljm.eWriteName(handle, "I2C_SCL_DIONUM", self._scl_pin)
                ljm.eWriteName(handle, "I2C_SPEED_THROTTLE", 0)
                ljm.eWriteName(handle, "I2C_OPTIONS", 0x04)  # clock stretching
                ljm.eWriteName(handle, "I2C_SLAVE_ADDRESS", addr)
                ljm.eWriteName(handle, "I2C_NUM_BYTES_TX", 0)
                ljm.eWriteName(handle, "I2C_NUM_BYTES_RX", 0)
                ljm.eWriteName(handle, "I2C_GO", 1)

                # Read ACKS register to verify actual acknowledgment.
                # I2C_ACKS reports the number of ACKs received. For a
                # 0-byte probe, only the address byte is sent. ACKS > 0
                # means the device responded with ACK.
                acks = int(ljm.eReadName(handle, "I2C_ACKS"))
                if acks > 0:
                    found.append(addr)
                    _debug(f"Device found at 0x{addr:02x} (ACKS={acks})")
            except Exception:
                # Bus error - no device at this address
                pass

        return found

    def read(
        self,
        address: int,
        num_bytes: int,
    ) -> List[int]:
        """
        Read bytes from an I2C device.

        Args:
            address: 7-bit I2C device address
            num_bytes: Number of bytes to read

        Returns:
            List of received bytes
        """
        return self._execute_transaction(
            address, tx_data=[], num_rx=num_bytes, options=0x04
        )

    def write(
        self,
        address: int,
        data: List[int],
    ) -> None:
        """
        Write bytes to an I2C device.

        Args:
            address: 7-bit I2C device address
            data: List of bytes to write
        """
        self._execute_transaction(
            address, tx_data=data, num_rx=0, options=0x04
        )

    def write_read(
        self,
        address: int,
        data: List[int],
        num_bytes: int,
    ) -> List[int]:
        """
        Write then read in a single I2C transaction (repeated start).

        The LabJack T7 handles the repeated start automatically when
        both TX and RX byte counts are non-zero.

        Args:
            address: 7-bit I2C device address
            data: List of bytes to write before reading
            num_bytes: Number of bytes to read after writing

        Returns:
            List of received bytes
        """
        return self._execute_transaction(
            address, tx_data=data, num_rx=num_bytes, options=0x04
        )
