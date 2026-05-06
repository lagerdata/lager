# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
LabJack T7 SPI driver implementing the SPIBase interface.

Provides SPI communication via LabJack T7 digital I/O pins using
the built-in SPI functionality.

LabJack T7 SPI Registers:
- SPI_CS_DIONUM: Chip select pin number (FIO/EIO/CIO/MIO number)
- SPI_CLK_DIONUM: Clock pin number
- SPI_MISO_DIONUM: MISO pin number
- SPI_MOSI_DIONUM: MOSI pin number
- SPI_MODE: SPI mode (0-3)
- SPI_SPEED_THROTTLE: Clock speed control (0=fastest, 65535=slowest)
- SPI_OPTIONS: Bit 0 = disable auto CS (0=on, 1=off), Bit 1 = CS active high
- SPI_NUM_BYTES: Number of bytes to transfer
- SPI_DATA_TX: Transmit buffer (write bytes here)
- SPI_DATA_RX: Receive buffer (read bytes from here)
- SPI_GO: Write 1 to execute transaction

Constraints:
- Maximum 56 bytes per transaction (hardware buffer limit)
- Full duplex only (reads and writes happen simultaneously)
- LabJack only supports MSB-first natively (LSB-first requires software reversal)
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional, Dict, Any

from .spi_base import SPIBase
from lager.exceptions import SPIBackendError

DEBUG = bool(os.environ.get("LAGER_SPI_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_SPI_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"SPI_DEBUG: {msg}\n")
        sys.stderr.flush()


# Maximum bytes per LabJack SPI transaction.
# The T7 SPI data buffers (SPI_DATA_TX at register 5010, SPI_DATA_RX at
# register 5050) are limited to 56 bytes each.  Transactions larger than
# 56 bytes fail with LJME_MBE2_ILLEGAL_DATA_ADDRESS (error 1202).
MAX_BYTES_PER_TRANSACTION = 56


class LabJackSPI(SPIBase):
    """
    LabJack T7 SPI implementation.

    Uses the LabJack T7's built-in SPI functionality to communicate
    with SPI slave devices.

    Pin configuration is provided via the net configuration 'params':
    - cs_pin: Chip select pin number (e.g., 0 for FIO0)
    - clk_pin: Clock pin number (e.g., 1 for FIO1)
    - mosi_pin: MOSI pin number (e.g., 2 for FIO2)
    - miso_pin: MISO pin number (e.g., 3 for FIO3)
    """

    # LabJack T7 SPI hardware dead zone: multi-byte transactions fail with
    # error 1239 (LJME_RECONNECT_FAILED) for throttle values in a range that
    # grows with transaction size.  Measured boundaries:
    #   4 bytes:  no dead zone
    #   16 bytes: boundary at ~54556
    #   32 bytes: boundary at ~60020
    #   56 bytes: boundary at ~62378
    # We use 62500 (with margin) to cover all sizes up to the 56-byte max.
    # Throttle 0 (~800 kHz) and throttle >= 62500 (~1 kHz and below) both work.
    _THROTTLE_DEAD_ZONE_END = 62500

    # Class-level flag: print dead-zone warning only once per session
    _throttle_warning_shown = False

    # Track last throttle value to skip redundant stabilization delays
    _last_throttle = None


    def __init__(
        self,
        cs_pin: Optional[int] = None,
        clk_pin: int = 1,
        mosi_pin: int = 2,
        miso_pin: int = 3,
        mode: int = 0,
        bit_order: str = "msb",
        frequency_hz: int = 1_000_000,
        word_size: int = 8,
        cs_active: str = "low",
        cs_mode: str = "auto",
    ):
        """
        Initialize LabJack SPI driver.

        Args:
            cs_pin: Chip select pin number (FIO/EIO/CIO/MIO number).
                    None when cs_mode is "manual" (3-pin SPI).
            clk_pin: Clock pin number
            mosi_pin: MOSI pin number
            miso_pin: MISO pin number
            mode: SPI mode (0-3)
            bit_order: "msb" or "lsb"
            frequency_hz: Clock frequency in Hz
            word_size: Bits per word (8, 16, or 32)
            cs_active: "low" or "high"
            cs_mode: "auto" (hardware CS) or "manual" (user-managed GPIO)
        """
        self._cs_pin = int(cs_pin) if cs_pin is not None else None
        self._clk_pin = int(clk_pin)
        self._mosi_pin = int(mosi_pin)
        self._miso_pin = int(miso_pin)

        # Configuration parameters
        self._mode = mode
        self._bit_order = bit_order.lower()
        self._frequency_hz = frequency_hz
        self._word_size = word_size
        self._cs_active = cs_active.lower()
        self._cs_mode = cs_mode.lower()

        # Validate parameters
        if self._cs_mode not in ("auto", "manual"):
            raise SPIBackendError(f"Invalid cs_mode '{cs_mode}'. Must be 'auto' or 'manual'.")
        if self._cs_mode == "auto" and self._cs_pin is None:
            raise SPIBackendError("cs_pin is required when cs_mode is 'auto'.")
        if self._mode not in (0, 1, 2, 3):
            raise SPIBackendError(f"Invalid SPI mode {mode}. Must be 0, 1, 2, or 3.")
        if self._bit_order not in ("msb", "lsb"):
            raise SPIBackendError(f"Invalid bit order '{bit_order}'. Must be 'msb' or 'lsb'.")
        if self._word_size not in (8, 16, 32):
            raise SPIBackendError(f"Invalid word size {word_size}. Must be 8, 16, or 32.")
        if self._cs_active not in ("low", "high"):
            raise SPIBackendError(f"Invalid CS active '{cs_active}'. Must be 'low' or 'high'.")

        _debug(f"LabJackSPI initialized: CS={cs_pin}, CLK={clk_pin}, "
               f"MOSI={mosi_pin}, MISO={miso_pin}, mode={mode}, "
               f"freq={frequency_hz}Hz, word_size={word_size}, "
               f"bit_order={bit_order}, cs_active={cs_active}, "
               f"cs_mode={cs_mode}")

        # Register pins with the conflict tracker so overlapping usage
        # within a single lager-python script produces a warning.
        try:
            from lager.io.labjack_handle import register_labjack_pins, PinRegistry
            pins = {
                PinRegistry.dio_to_name(self._clk_pin): "CLK",
                PinRegistry.dio_to_name(self._mosi_pin): "MOSI",
                PinRegistry.dio_to_name(self._miso_pin): "MISO",
            }
            if self._cs_mode != "manual" and self._cs_pin is not None:
                pins[PinRegistry.dio_to_name(self._cs_pin)] = "CS"
            register_labjack_pins("SPI", pins)
        except Exception:
            pass  # Don't let registration failure break SPI

    def _get_handle(self) -> int:
        """Get LabJack handle from the global handle manager."""
        from lager.io.labjack_handle import get_labjack_handle
        return get_labjack_handle()

    def _get_ljm(self):
        """Get the ljm module."""
        from lager.io.labjack_handle import ljm, _LJM_ERR
        if ljm is None:
            raise SPIBackendError(f"LabJack LJM library not available: {_LJM_ERR}")
        return ljm

    def _get_cs_dio_value(self) -> int:
        """
        Return the DIO number to write to SPI_CS_DIONUM.

        When cs_pin is set, return it directly.
        When cs_pin is None (manual CS mode), the LabJack SPI register still
        requires a value for SPI_CS_DIONUM.  Use clk_pin as a safe dummy --
        with auto_cs disabled the hardware never actually asserts this pin as CS.
        """
        if self._cs_pin is not None:
            return self._cs_pin
        return self._clk_pin

    def _frequency_to_throttle(self, frequency_hz: int, num_bytes: int = 1) -> int:
        """
        Convert frequency in Hz to LabJack SPI_SPEED_THROTTLE value.

        LabJack T7 SPI clock formula (approximate):
        - Throttle 0 = ~800 kHz (fastest)
        - Throttle 65535 = ~14 Hz (slowest)

        The relationship is roughly:
        frequency = 800000 / (1 + throttle * 0.012)

        Solving for throttle:
        throttle = (800000 / frequency - 1) / 0.012

        Note: These are approximations. Actual frequencies may vary.

        For multi-byte transactions (num_bytes >= 2), throttle values in the
        dead zone (1 to ~62499) cause error 1239.  These are clamped to the
        slow boundary (throttle 62500, ~1 kHz) to avoid running faster than
        requested.  For 800 kHz, request frequency >= 800000 Hz.
        """
        if frequency_hz >= 800_000:
            return 0  # Maximum speed

        if frequency_hz <= 14:
            throttle = 65535  # Minimum speed
        else:
            # Calculate throttle value
            throttle = int((800_000 / frequency_hz - 1) / 0.012)
            # Clamp to valid range
            throttle = max(0, min(65535, throttle))

        # For multi-byte transactions, clamp throttle out of the dead zone.
        # Single-byte transactions work at any throttle.
        # Always clamp to the slow boundary -- users requesting sub-800kHz
        # speeds want slower, not faster.  Running faster risks data corruption.
        if num_bytes >= 2 and 1 <= throttle < self._THROTTLE_DEAD_ZONE_END:
            clamped = self._THROTTLE_DEAD_ZONE_END
            if not LabJackSPI._throttle_warning_shown:
                sys.stderr.write(
                    f"WARNING: LabJack SPI throttle {throttle} (requested "
                    f"{frequency_hz}Hz) is in the hardware dead zone "
                    f"(1-{self._THROTTLE_DEAD_ZONE_END - 1}) for multi-byte "
                    f"transactions. Clamping to throttle={clamped} (~1kHz). "
                    f"For 800kHz, request frequency >= 800000Hz.\n"
                )
                sys.stderr.flush()
                LabJackSPI._throttle_warning_shown = True
            _debug(f"Frequency {frequency_hz}Hz -> throttle {throttle} "
                   f"(clamped from dead zone to {clamped})")
            return clamped

        _debug(f"Frequency {frequency_hz}Hz -> throttle {throttle}")
        return throttle

    def _setup_spi_registers(self, handle: int, ljm, num_bytes: int, auto_cs: bool = True) -> None:
        """
        Configure LabJack SPI registers for a transaction.

        Args:
            handle: LabJack device handle
            ljm: LabJack module
            num_bytes: Number of bytes for this transaction
            auto_cs: Whether to use automatic CS control
        """
        import time

        # Configure SPI pins
        ljm.eWriteName(handle, "SPI_CS_DIONUM", self._get_cs_dio_value())
        ljm.eWriteName(handle, "SPI_CLK_DIONUM", self._clk_pin)
        ljm.eWriteName(handle, "SPI_MISO_DIONUM", self._miso_pin)
        ljm.eWriteName(handle, "SPI_MOSI_DIONUM", self._mosi_pin)

        # Configure SPI mode (0-3)
        ljm.eWriteName(handle, "SPI_MODE", self._mode)

        # Configure speed throttle
        throttle = self._frequency_to_throttle(self._frequency_hz, num_bytes)
        ljm.eWriteName(handle, "SPI_SPEED_THROTTLE", throttle)

        # For low frequencies (high throttle), add a small delay to let
        # the LabJack stabilize after throttle configuration (only on change)
        if throttle > 0 and throttle != LabJackSPI._last_throttle:
            time.sleep(0.05)  # 50ms stabilization delay
        LabJackSPI._last_throttle = throttle

        # Configure options
        # Bit 0: Disable auto CS (0 = auto CS on, 1 = auto CS off per LabJack docs)
        # Bit 1: CS active high (1 = active high, 0 = active low)
        options = 0
        if not (auto_cs and self._cs_mode != "manual"):
            options |= 0x01  # Disable auto CS (we use manual GPIO instead)
        if self._cs_active == "high":
            options |= 0x02  # CS active high
        ljm.eWriteName(handle, "SPI_OPTIONS", options)

        # Set number of bytes
        ljm.eWriteName(handle, "SPI_NUM_BYTES", num_bytes)

        _debug(f"SPI registers configured: mode={self._mode}, throttle={throttle}, "
               f"options={options:#04x}, num_bytes={num_bytes}")

    def _reverse_byte(self, value: int) -> int:
        """Reverse bits in a byte for LSB-first mode."""
        return self.reverse_bits(value, 8)

    def _reverse_word(self, value: int) -> int:
        """Reverse bits in a word for LSB-first mode."""
        return self.reverse_bits(value, self._word_size)

    def _words_to_bytes(self, words: List[int]) -> List[int]:
        """
        Convert words to bytes based on word_size and bit_order.

        For word_size > 8, each word is split into multiple bytes.
        MSB-first: Most significant byte first
        LSB-first: Least significant byte first (bits reversed)
        """
        max_value = (1 << self._word_size) - 1
        for w in words:
            if w > max_value:
                raise SPIBackendError(
                    f"Data value 0x{w:X} exceeds {self._word_size}-bit word size "
                    f"(max 0x{max_value:X}). Use commas to separate into "
                    f"{self._word_size}-bit values, or set --word-size to match "
                    f"your data."
                )

        if self._word_size == 8:
            # For 8-bit words, optionally reverse bits for LSB-first
            if self._bit_order == "lsb":
                return [self._reverse_byte(w & 0xFF) for w in words]
            return [w & 0xFF for w in words]

        bytes_per_word = self._word_size // 8
        result = []

        for word in words:
            if self._bit_order == "lsb":
                word = self._reverse_word(word)

            # Split word into bytes (MSB first for transmission)
            word_bytes = []
            for i in range(bytes_per_word - 1, -1, -1):
                word_bytes.append((word >> (i * 8)) & 0xFF)
            result.extend(word_bytes)

        return result

    def _bytes_to_words(self, data_bytes: List[int]) -> List[int]:
        """
        Convert received bytes back to words based on word_size and bit_order.
        """
        if self._word_size == 8:
            if self._bit_order == "lsb":
                return [self._reverse_byte(b) for b in data_bytes]
            return list(data_bytes)

        bytes_per_word = self._word_size // 8
        result = []

        for i in range(0, len(data_bytes), bytes_per_word):
            word = 0
            for j in range(bytes_per_word):
                if i + j < len(data_bytes):
                    word = (word << 8) | data_bytes[i + j]
            if self._bit_order == "lsb":
                word = self._reverse_word(word)
            result.append(word)

        return result

    def _execute_transaction(
        self,
        tx_bytes: List[int],
        keep_cs: bool = False,
        max_retries: int = 5,
    ) -> List[int]:
        """
        Execute a single SPI transaction with retry logic.

        Args:
            tx_bytes: Bytes to transmit
            keep_cs: If True, keep CS asserted after transfer
            max_retries: Maximum number of retry attempts for reconnect errors

        Returns:
            List of received bytes
        """
        import time

        num_bytes = len(tx_bytes)
        if num_bytes > MAX_BYTES_PER_TRANSACTION:
            raise SPIBackendError(
                f"Transaction size {num_bytes} exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes. Split into multiple transactions."
            )

        last_error = None
        throttle = self._frequency_to_throttle(self._frequency_hz, num_bytes)

        for attempt in range(max_retries):
            try:
                handle = self._get_handle()
                ljm = self._get_ljm()

                # For low frequencies (high throttle), set longer send/receive timeout
                if throttle > 0:
                    try:
                        ljm.writeLibraryConfigS("LJM_SEND_RECEIVE_TIMEOUT_MS", 10000)
                    except Exception as e:
                        _debug(f"Could not set timeout: {e}")

                # Use LabJack auto CS (SPI_OPTIONS bit 0 = 0) for normal
                # transactions.  Manual CS (bit 0 = 1) is only needed for
                # keep_cs, where CS must stay asserted between transactions.
                # Using auto CS avoids error 1239 at throttle > 0.
                use_auto_cs = (not keep_cs) and self._cs_mode != "manual"
                self._setup_spi_registers(handle, ljm, num_bytes, auto_cs=use_auto_cs)

                # When using manual CS, assert it before the transaction
                if not use_auto_cs and self._cs_mode != "manual" and self._cs_pin is not None:
                    cs_assert = 0 if self._cs_active == "low" else 1
                    ljm.eWriteName(handle, f"FIO{self._cs_pin}", cs_assert)

                # Write TX data and execute transaction
                _debug(f"TX bytes: {[hex(b) for b in tx_bytes]}")
                ljm.eWriteNameByteArray(handle, "SPI_DATA_TX", num_bytes, tx_bytes)
                ljm.eWriteName(handle, "SPI_GO", 1)

                # Wait for SPI transaction to complete
                # SPI_GO reads back as 0 when the transaction is done
                # At low frequencies, this can take a while (e.g., 100kHz with 100 bytes = 8ms)
                max_wait_ms = 1000  # 1 second timeout (generous for slow SPI)
                poll_interval = 0.001  # 1ms polling interval
                start_time = time.time()

                while True:
                    go_status = ljm.eReadName(handle, "SPI_GO")
                    if go_status == 0:
                        _debug("SPI transaction complete")
                        break
                    elapsed_ms = (time.time() - start_time) * 1000
                    if elapsed_ms > max_wait_ms:
                        raise SPIBackendError(
                            f"SPI transaction timeout after {max_wait_ms}ms. "
                            f"The transaction may not have completed."
                        )
                    time.sleep(poll_interval)

                # Read RX data
                rx_bytes = ljm.eReadNameByteArray(handle, "SPI_DATA_RX", num_bytes)
                _debug(f"RX bytes: {[hex(b) for b in rx_bytes]}")

                # Deassert CS manually only when we asserted it manually
                if not use_auto_cs and not keep_cs and self._cs_mode != "manual" and self._cs_pin is not None:
                    cs_deassert = 1 if self._cs_active == "low" else 0
                    ljm.eWriteName(handle, f"FIO{self._cs_pin}", cs_deassert)

                return list(rx_bytes)

            except Exception as e:
                last_error = e
                error_str = str(e)

                # Check for recoverable errors:
                # - 1227: LJME_DEVICE_NOT_FOUND (device initializing)
                # - 1239: LJME_RECONNECT_FAILED (connection lost)
                is_recoverable = (
                    "1227" in error_str or
                    "1239" in error_str or
                    "RECONNECT" in error_str.upper() or
                    "NOT_FOUND" in error_str.upper()
                )
                if is_recoverable:
                    _debug(f"Reconnect error on attempt {attempt + 1}/{max_retries}: {e}")

                    if attempt < max_retries - 1:
                        # DON'T force close the handle - just wait and retry.
                        # Calling force_close or closeAll puts the LabJack in a bad state
                        # where low-frequency SPI (high throttle) fails.
                        # The handle manager will detect if the handle is truly stale
                        # and reconnect on the next get_handle() call.

                        # Wait with exponential backoff (2s, 4s, 8s, 16s)
                        wait_time = 2.0 * (2 ** attempt)
                        _debug(f"Waiting {wait_time}s before retry (no handle close)...")
                        time.sleep(wait_time)
                        continue
                    else:
                        raise SPIBackendError(
                            f"LabJack device error after {max_retries} attempts. "
                            f"Ensure the device is connected and try again. Error: {e}"
                        )
                else:
                    # Non-reconnect error, don't retry
                    raise SPIBackendError(f"SPI transaction failed: {e}")

        # Should not reach here, but just in case
        raise SPIBackendError(f"SPI transaction failed: {last_error}")

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
        Configure SPI parameters.

        Only explicitly-provided parameters are updated; omitted parameters
        retain their current values.

        Args:
            mode: SPI mode (0-3)
            bit_order: "msb" or "lsb"
            frequency_hz: Clock frequency in Hz
            word_size: Bits per word (8, 16, or 32)
            cs_active: "low" or "high"
            cs_mode: "auto" (hardware CS) or "manual" (user-managed GPIO)
        """
        # Validate and apply only explicitly-provided parameters
        if mode is not None:
            if mode not in (0, 1, 2, 3):
                raise SPIBackendError(f"Invalid SPI mode {mode}. Must be 0, 1, 2, or 3.")
            self._mode = mode
        if bit_order is not None:
            if bit_order.lower() not in ("msb", "lsb"):
                raise SPIBackendError(f"Invalid bit order '{bit_order}'. Must be 'msb' or 'lsb'.")
            self._bit_order = bit_order.lower()
        if frequency_hz is not None:
            self._frequency_hz = frequency_hz
        if word_size is not None:
            if word_size not in (8, 16, 32):
                raise SPIBackendError(f"Invalid word size {word_size}. Must be 8, 16, or 32.")
            self._word_size = word_size
        if cs_active is not None:
            if cs_active.lower() not in ("low", "high"):
                raise SPIBackendError(f"Invalid CS active '{cs_active}'. Must be 'low' or 'high'.")
            self._cs_active = cs_active.lower()
        if cs_mode is not None:
            if cs_mode.lower() not in ("auto", "manual"):
                raise SPIBackendError(f"Invalid cs_mode '{cs_mode}'. Must be 'auto' or 'manual'.")
            if cs_mode.lower() == "auto" and self._cs_pin is None:
                raise SPIBackendError("Cannot switch to cs_mode 'auto': no CS pin configured on this net.")
            self._cs_mode = cs_mode.lower()

        _debug(f"SPI configured: mode={self._mode}, freq={self._frequency_hz}Hz, "
               f"word_size={self._word_size}, bit_order={self._bit_order}, "
               f"cs_active={self._cs_active}, cs_mode={self._cs_mode}")

    def read(
        self,
        n_words: int,
        fill: int = 0xFF,
        keep_cs: bool = False,
    ) -> List[int]:
        """
        Read data from SPI slave.

        Args:
            n_words: Number of words to read
            fill: Fill byte/word to send while reading
            keep_cs: If True, keep CS asserted after transfer

        Returns:
            List of received words
        """
        # Validate size before attempting transaction
        bytes_per_word = self._word_size // 8
        total_bytes = n_words * bytes_per_word
        if total_bytes > MAX_BYTES_PER_TRANSACTION:
            max_words = MAX_BYTES_PER_TRANSACTION // bytes_per_word
            raise SPIBackendError(
                f"Transaction size {n_words} words ({total_bytes} bytes) exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes. Use at most {max_words} words with "
                f"{self._word_size}-bit word size, or split into multiple transactions."
            )

        # Create fill data
        tx_words = [fill] * n_words

        return self.read_write(tx_words, keep_cs=keep_cs)

    def read_write(
        self,
        data: List[int],
        keep_cs: bool = False,
    ) -> List[int]:
        """
        Perform simultaneous read/write SPI transfer.

        Args:
            data: List of words to transmit
            keep_cs: If True, keep CS asserted after transfer

        Returns:
            List of received words
        """
        if not data:
            return []

        # Convert words to bytes
        tx_bytes = self._words_to_bytes(data)

        # Validate size and give helpful error message
        if len(tx_bytes) > MAX_BYTES_PER_TRANSACTION:
            bytes_per_word = self._word_size // 8
            max_words = MAX_BYTES_PER_TRANSACTION // bytes_per_word
            raise SPIBackendError(
                f"Transaction size {len(data)} words ({len(tx_bytes)} bytes) exceeds maximum of "
                f"{MAX_BYTES_PER_TRANSACTION} bytes. Use at most {max_words} words with "
                f"{self._word_size}-bit word size, or split into multiple transactions."
            )

        # Execute the transaction
        rx_bytes = self._execute_transaction(tx_bytes, keep_cs=keep_cs)

        # Convert bytes back to words
        return self._bytes_to_words(rx_bytes)
