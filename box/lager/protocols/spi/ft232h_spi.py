# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
FTDI FT232H SPI driver implementing the SPIBase interface.

Provides SPI communication via the FT232H USB-to-MPSSE adapter
(VID:PID 0403:6014) using the pyftdi library.

The FT232H has fixed MPSSE pin assignments for SPI:
- AD0: SCK  (clock)
- AD1: MOSI (master out, slave in)
- AD2: MISO (master in, slave out)
- AD3: CS   (chip select, active low by default)

Additional CS lines can use AD4-AD7.

Supports:
- SPI modes 0-3
- Clock frequencies up to ~30 MHz
- MSB-first and LSB-first (software bit reversal for >8-bit words)
- Configurable CS polarity
"""
from __future__ import annotations

import atexit
import os
import sys
import time
from typing import List, Optional

from .spi_base import SPIBase
from lager.exceptions import SPIBackendError

DEBUG = bool(os.environ.get("LAGER_SPI_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_SPI_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"SPI_DEBUG(FT232H): {msg}\n")
        sys.stderr.flush()


# Maximum retries for USB disconnect recovery
MAX_USB_RETRIES = 3


class FT232HSPI(SPIBase):
    """
    FTDI FT232H SPI implementation using pyftdi.

    Uses the FT232H's MPSSE engine for SPI communication.
    The FT232H has fixed hardware SPI pins so no pin configuration is needed.
    """

    def __init__(
        self,
        serial: Optional[str] = None,
        cs_pin: int = 3,
        mode: int = 0,
        bit_order: str = "msb",
        frequency_hz: int = 1_000_000,
        word_size: int = 8,
        cs_active: str = "low",
    ):
        """
        Initialize FT232H SPI driver.

        Args:
            serial: USB serial number string (for multi-device setups).
                    If None, uses the first available FT232H.
            cs_pin: Which AD pin to use for chip select (default 3 = AD3).
                    Valid range: 3-7 (AD3 through AD7).
            mode: SPI mode (0-3).
            bit_order: "msb" or "lsb".
            frequency_hz: Clock frequency in Hz (default 1 MHz).
            word_size: Bits per word (8, 16, or 32).
            cs_active: "low" or "high".
        """
        self._serial = serial
        self._cs_pin = cs_pin
        self._mode = mode
        self._bit_order = bit_order.lower()
        self._frequency_hz = frequency_hz
        self._word_size = word_size
        self._cs_active = cs_active.lower()

        self._controller = None
        self._port = None

        # Validate parameters
        if self._mode not in (0, 1, 2, 3):
            raise SPIBackendError(f"Invalid SPI mode {mode}. Must be 0, 1, 2, or 3.")
        if self._bit_order not in ("msb", "lsb"):
            raise SPIBackendError(f"Invalid bit order '{bit_order}'. Must be 'msb' or 'lsb'.")
        if self._word_size not in (8, 16, 32):
            raise SPIBackendError(f"Invalid word size {word_size}. Must be 8, 16, or 32.")
        if self._cs_active not in ("low", "high"):
            raise SPIBackendError(f"Invalid CS active '{cs_active}'. Must be 'low' or 'high'.")

        _debug(f"FT232HSPI initialized: serial={serial}, cs_pin={cs_pin}, "
               f"mode={mode}, freq={frequency_hz}Hz, word_size={word_size}, "
               f"bit_order={bit_order}, cs_active={cs_active}")

    @staticmethod
    def _get_pyftdi():
        """Lazy-import the pyftdi SPI module."""
        try:
            from pyftdi.spi import SpiController
            return SpiController
        except ImportError:
            raise SPIBackendError(
                "pyftdi library not available. "
                "Install with: pip install pyftdi"
            )

    def _build_url(self) -> str:
        """Build the pyftdi device URL."""
        if self._serial:
            return f"ftdi://ftdi:232h:{self._serial}/1"
        return "ftdi://ftdi:232h/1"

    def _ensure_open(self):
        """
        Open the FT232H SPI controller if not already open.

        Returns the SpiPort instance.
        """
        if self._port is not None:
            return self._port

        SpiController = self._get_pyftdi()

        url = self._build_url()
        _debug(f"Opening FT232H SPI at URL: {url}")

        try:
            ctrl = SpiController()
            ctrl.configure(url)
        except Exception as exc:
            raise SPIBackendError(
                f"Failed to open FT232H SPI at {url}: {exc}"
            ) from exc

        self._controller = ctrl
        atexit.register(self._close)

        # Get SPI port with the current configuration
        # cs_pin is relative to AD3 (cs=0 -> AD3, cs=1 -> AD4, etc.)
        cs = self._cs_pin - 3
        if cs < 0:
            cs = 0

        try:
            self._port = ctrl.get_port(
                cs=cs,
                freq=self._frequency_hz,
                mode=self._mode,
            )
        except Exception as exc:
            self._controller = None
            raise SPIBackendError(
                f"Failed to configure FT232H SPI port: {exc}"
            ) from exc

        _debug(f"FT232H SPI opened: mode={self._mode}, freq={self._frequency_hz}Hz, "
               f"cs_pin=AD{self._cs_pin}")

        return self._port

    def _close(self):
        """Close the FT232H SPI controller."""
        if self._controller is not None:
            try:
                self._controller.close()
                _debug("FT232H SPI closed")
            except Exception as e:
                _debug(f"Error closing FT232H SPI: {e}")
            finally:
                self._controller = None
                self._port = None

    def _reconnect(self):
        """
        Close and reopen the FT232H for USB disconnect recovery.
        """
        _debug("Attempting reconnect after USB error...")
        self._controller = None
        self._port = None
        self._ensure_open()

    def config(
        self,
        mode: int = None,
        bit_order: str = None,
        frequency_hz: int = None,
        word_size: int = None,
        cs_active: str = None,
    ) -> None:
        """
        Configure SPI parameters.

        Only explicitly-provided parameters are updated; omitted parameters
        retain their current values.

        Args:
            mode: SPI mode (0-3).
            bit_order: "msb" or "lsb".
            frequency_hz: Clock frequency in Hz.
            word_size: Bits per word (8, 16, or 32).
            cs_active: "low" or "high".
        """
        changed = False
        if mode is not None:
            if mode not in (0, 1, 2, 3):
                raise SPIBackendError(f"Invalid SPI mode {mode}. Must be 0, 1, 2, or 3.")
            self._mode = mode
            changed = True
        if bit_order is not None:
            if bit_order.lower() not in ("msb", "lsb"):
                raise SPIBackendError(f"Invalid bit order '{bit_order}'. Must be 'msb' or 'lsb'.")
            self._bit_order = bit_order.lower()
            changed = True
        if frequency_hz is not None:
            self._frequency_hz = frequency_hz
            changed = True
        if word_size is not None:
            if word_size not in (8, 16, 32):
                raise SPIBackendError(f"Invalid word size {word_size}. Must be 8, 16, or 32.")
            self._word_size = word_size
            changed = True
        if cs_active is not None:
            if cs_active.lower() not in ("low", "high"):
                raise SPIBackendError(f"Invalid CS active '{cs_active}'. Must be 'low' or 'high'.")
            self._cs_active = cs_active.lower()
            changed = True

        # Reconfigure if already open (need to get a new port with new settings)
        if changed and self._controller is not None:
            self._close()
            self._ensure_open()

        _debug(f"SPI configured: mode={self._mode}, freq={self._frequency_hz}Hz, "
               f"word_size={self._word_size}, bit_order={self._bit_order}, "
               f"cs_active={self._cs_active}")

    def _words_to_bytes(self, words: List[int]) -> List[int]:
        """
        Convert words to bytes based on word_size and bit_order.

        For word_size > 8 with LSB-first: software bit reversal is applied.
        """
        if self._word_size == 8:
            if self._bit_order == "lsb":
                return [self.reverse_bits(w & 0xFF, 8) for w in words]
            return [w & 0xFF for w in words]

        bytes_per_word = self._word_size // 8
        result = []

        for word in words:
            if self._bit_order == "lsb":
                word = self.reverse_bits(word, self._word_size)

            # Split word into bytes (MSB first for transmission)
            for i in range(bytes_per_word - 1, -1, -1):
                result.append((word >> (i * 8)) & 0xFF)

        return result

    def _bytes_to_words(self, data_bytes: List[int]) -> List[int]:
        """
        Convert received bytes back to words based on word_size and bit_order.
        """
        if self._word_size == 8:
            if self._bit_order == "lsb":
                return [self.reverse_bits(b, 8) for b in data_bytes]
            return list(data_bytes)

        bytes_per_word = self._word_size // 8
        result = []

        for i in range(0, len(data_bytes), bytes_per_word):
            word = 0
            for j in range(bytes_per_word):
                if i + j < len(data_bytes):
                    word = (word << 8) | data_bytes[i + j]
            if self._bit_order == "lsb":
                word = self.reverse_bits(word, self._word_size)
            result.append(word)

        return result

    def _execute_transaction(
        self,
        tx_bytes: List[int],
        keep_cs: bool = False,
        compensate_cpol: bool = True,
    ) -> List[int]:
        """
        Execute a single SPI transaction with retry logic for USB errors.

        Args:
            tx_bytes: Bytes to transmit.
            keep_cs: If True, keep CS asserted after transfer.
            compensate_cpol: If True and mode is CPOL=1, apply the
                bit-shift compensation for the pyftdi SCK idle bug.
                Set to False for write-only operations where RX data
                is discarded (avoids sending an extra byte to the slave).

        Returns:
            List of received bytes.
        """
        need_shift = compensate_cpol and self._mode in (2, 3)
        last_error = None

        for attempt in range(MAX_USB_RETRIES):
            try:
                port = self._ensure_open()

                if need_shift:
                    # CPOL=1 RX compensation: pyftdi starts SCK LOW instead
                    # of HIGH, so the first rising clock edge is spurious.
                    # The slave hasn't shifted out data yet, causing all
                    # received bits to be offset right by 1.  Send one extra
                    # fill byte so the last real bit is captured, then shift
                    # the whole RX buffer left by 1 to realign.
                    padded_tx = list(tx_bytes) + [0xFF]

                    _debug(f"TX bytes ({len(tx_bytes)}+1 pad): "
                           f"{[hex(b) for b in padded_tx[:32]]}"
                           f"{'...' if len(padded_tx) > 32 else ''}")

                    rx_data = port.exchange(
                        bytes(padded_tx),
                        len(padded_tx),
                        duplex=True,
                    )
                    rx_all = list(rx_data)

                    _debug(f"RX raw  ({len(rx_all)}): "
                           f"{[hex(b) for b in rx_all[:32]]}"
                           f"{'...' if len(rx_all) > 32 else ''}")

                    # Shift left by 1 bit across all bytes (carry from next)
                    n = len(tx_bytes)
                    rx_bytes = []
                    for i in range(n):
                        b = (rx_all[i] << 1) & 0xFF
                        if i + 1 < len(rx_all):
                            b |= (rx_all[i + 1] >> 7) & 0x01
                        rx_bytes.append(b)

                    _debug(f"RX comp ({len(rx_bytes)}): "
                           f"{[hex(b) for b in rx_bytes[:32]]}"
                           f"{'...' if len(rx_bytes) > 32 else ''}")
                else:
                    _debug(f"TX bytes ({len(tx_bytes)}): "
                           f"{[hex(b) for b in tx_bytes[:32]]}"
                           f"{'...' if len(tx_bytes) > 32 else ''}")

                    rx_data = port.exchange(
                        bytes(tx_bytes),
                        len(tx_bytes),
                        duplex=True,
                    )
                    rx_bytes = list(rx_data)

                    _debug(f"RX bytes ({len(rx_bytes)}): "
                           f"{[hex(b) for b in rx_bytes[:32]]}"
                           f"{'...' if len(rx_bytes) > 32 else ''}")

                return rx_bytes
            except SPIBackendError:
                raise
            except Exception as e:
                last_error = e
                _debug(f"USB error on SPI attempt {attempt + 1}/{MAX_USB_RETRIES}: {e}")
                if attempt < MAX_USB_RETRIES - 1:
                    time.sleep(1.0 * (2 ** attempt))
                    self._reconnect()
                    continue
                raise SPIBackendError(
                    f"SPI transaction failed after "
                    f"{MAX_USB_RETRIES} attempts: {last_error}"
                )

    def read(
        self,
        n_words: int,
        fill: int = 0xFF,
        keep_cs: bool = False,
    ) -> List[int]:
        """
        Read data from SPI slave.

        Args:
            n_words: Number of words to read.
            fill: Fill byte/word to send while reading.
            keep_cs: If True, keep CS asserted after transfer.

        Returns:
            List of received words.
        """
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
            data: List of words to transmit.
            keep_cs: If True, keep CS asserted after transfer.

        Returns:
            List of received words.
        """
        if not data:
            return []

        tx_bytes = self._words_to_bytes(data)
        rx_bytes = self._execute_transaction(tx_bytes, keep_cs=keep_cs)
        return self._bytes_to_words(rx_bytes)

    def write(
        self,
        data: List[int],
        keep_cs: bool = False,
    ) -> None:
        """
        Write data to SPI slave (discard received data).

        Overrides base class to skip CPOL=1 RX compensation so that
        no extra byte is sent to the slave during write operations.
        """
        if not data:
            return
        tx_bytes = self._words_to_bytes(data)
        self._execute_transaction(tx_bytes, keep_cs=keep_cs, compensate_cpol=False)

    def __del__(self):
        """Clean up: close device on garbage collection."""
        self._close()
