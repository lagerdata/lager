# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Abstract base class for SPI drivers.

Defines the interface that all SPI implementations must follow.
Currently implemented by LabJackSPI, with future support planned
for Aardvark and FTDI adapters.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from lager.exceptions import SPIBackendError


class SPIBase(ABC):
    """
    Abstract base class for SPI communication drivers.

    SPI (Serial Peripheral Interface) is a synchronous serial communication
    protocol using four signals:
    - SCLK: Serial Clock
    - MOSI: Master Out Slave In (data from master to slave)
    - MISO: Master In Slave Out (data from slave to master)
    - CS: Chip Select (active low or high, configurable)

    All implementations must support the config(), read(), and read_write()
    methods defined here.
    """

    @abstractmethod
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
            mode: SPI mode (0-3) defining clock polarity and phase
                - Mode 0: CPOL=0, CPHA=0 (clock idle low, sample on rising edge)
                - Mode 1: CPOL=0, CPHA=1 (clock idle low, sample on falling edge)
                - Mode 2: CPOL=1, CPHA=0 (clock idle high, sample on falling edge)
                - Mode 3: CPOL=1, CPHA=1 (clock idle high, sample on rising edge)
            bit_order: "msb" for MSB-first or "lsb" for LSB-first
            frequency_hz: SPI clock frequency in Hz (e.g., 1_000_000 for 1 MHz)
            word_size: Number of bits per word (8, 16, or 32)
            cs_active: Chip select polarity - "low" (active low) or "high" (active high)
            cs_mode: CS assertion mode - "auto" (hardware SS) or "manual"
                     (user-managed GPIO). Supported by Aardvark and LabJack T7 drivers.
        """
        pass

    @abstractmethod
    def read(
        self,
        n_words: int,
        fill: int = 0xFF,
        keep_cs: bool = False,
    ) -> List[int]:
        """
        Read data from SPI slave (send fill bytes while reading).

        Args:
            n_words: Number of words to read
            fill: Fill byte/word to send while reading (default 0xFF)
            keep_cs: If True, keep chip select asserted after transfer

        Returns:
            List of received words as integers
        """
        pass

    @abstractmethod
    def read_write(
        self,
        data: List[int],
        keep_cs: bool = False,
    ) -> List[int]:
        """
        Perform simultaneous read/write SPI transfer.

        SPI is full-duplex, so data is sent and received simultaneously.
        The number of bytes/words received equals the number sent.

        Args:
            data: List of bytes/words to transmit
            keep_cs: If True, keep chip select asserted after transfer

        Returns:
            List of received words as integers
        """
        pass

    def transfer(
        self,
        data: List[int],
        keep_cs: bool = False,
    ) -> List[int]:
        """
        Alias for read_write() for compatibility.

        Args:
            data: List of bytes/words to transmit
            keep_cs: If True, keep chip select asserted after transfer

        Returns:
            List of received words as integers
        """
        return self.read_write(data, keep_cs=keep_cs)

    def write(
        self,
        data: List[int],
        keep_cs: bool = False,
    ) -> None:
        """
        Write data to SPI slave (discard received data).

        This is a convenience method that calls read_write() and
        discards the received data.

        Args:
            data: List of bytes/words to transmit
            keep_cs: If True, keep chip select asserted after transfer
        """
        self.read_write(data, keep_cs=keep_cs)

    @staticmethod
    def reverse_bits(value: int, bit_count: int = 8) -> int:
        """
        Reverse the bits in a value (for LSB-first mode).

        Args:
            value: The value to reverse
            bit_count: Number of bits to reverse (8, 16, or 32)

        Returns:
            The value with bits reversed
        """
        result = 0
        for _ in range(bit_count):
            result = (result << 1) | (value & 1)
            value >>= 1
        return result

    @staticmethod
    def words_to_bytes(
        words: List[int],
        word_size: int = 8,
        bit_order: str = "msb",
    ) -> List[int]:
        """
        Pack words into the byte stream that goes out on the wire.

        Lives here rather than on a driver because it is pure arithmetic: every
        LabJack part is MSB-first in firmware, so LSB-first is software on all
        of them, and a second copy of the bit reversal that disagrees with this
        one would be invisible until it reached a scope.

        Words are split most-significant-byte first. For LSB-first the whole
        word is reversed before splitting, not each byte independently.

        Args:
            words: Values to transmit, each within *word_size* bits
            word_size: Bits per word (8, 16, or 32)
            bit_order: "msb" or "lsb"

        Returns:
            Flat list of bytes

        Raises:
            SPIBackendError: A value does not fit in *word_size* bits
        """
        max_value = (1 << word_size) - 1
        for w in words:
            if w > max_value:
                raise SPIBackendError(
                    f"Data value 0x{w:X} exceeds {word_size}-bit word size "
                    f"(max 0x{max_value:X}). Use commas to separate into "
                    f"{word_size}-bit values, or set --word-size to match "
                    f"your data."
                )

        if word_size == 8:
            if bit_order == "lsb":
                return [SPIBase.reverse_bits(w & 0xFF, 8) for w in words]
            return [w & 0xFF for w in words]

        bytes_per_word = word_size // 8
        result: List[int] = []
        for word in words:
            if bit_order == "lsb":
                word = SPIBase.reverse_bits(word, word_size)
            for i in range(bytes_per_word - 1, -1, -1):
                result.append((word >> (i * 8)) & 0xFF)
        return result

    @staticmethod
    def bytes_to_words(
        data_bytes: List[int],
        word_size: int = 8,
        bit_order: str = "msb",
    ) -> List[int]:
        """
        Reassemble received bytes into words. The inverse of words_to_bytes.

        A trailing group of bytes too short to fill a word becomes a short word
        rather than being dropped -- a truncated read is a real thing to hand
        back, and discarding it would hide it.

        Args:
            data_bytes: Bytes as received
            word_size: Bits per word (8, 16, or 32)
            bit_order: "msb" or "lsb"

        Returns:
            List of words
        """
        if word_size == 8:
            if bit_order == "lsb":
                return [SPIBase.reverse_bits(b, 8) for b in data_bytes]
            return list(data_bytes)

        bytes_per_word = word_size // 8
        result: List[int] = []
        for i in range(0, len(data_bytes), bytes_per_word):
            word = 0
            for j in range(bytes_per_word):
                if i + j < len(data_bytes):
                    word = (word << 8) | data_bytes[i + j]
            if bit_order == "lsb":
                word = SPIBase.reverse_bits(word, word_size)
            result.append(word)
        return result
