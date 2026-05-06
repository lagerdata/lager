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
