# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Abstract base class for I2C drivers.

Defines the interface that all I2C implementations must follow.
Currently implemented by LabJackI2C and AardvarkI2C.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional


class I2CBase(ABC):
    """
    Abstract base class for I2C communication drivers.

    I2C (Inter-Integrated Circuit) is a synchronous serial communication
    protocol using two signals:
    - SDA: Serial Data (bidirectional)
    - SCL: Serial Clock

    All implementations must support the config(), scan(), read(), write(),
    and write_read() methods defined here.
    """

    @staticmethod
    def _validate_address(address: int) -> None:
        """
        Validate a 7-bit I2C address is in the legal range.

        Args:
            address: 7-bit I2C device address

        Raises:
            ValueError: If address is outside 0x00-0x7F
        """
        if not isinstance(address, int) or address < 0x00 or address > 0x7F:
            raise ValueError(
                f"Invalid I2C address: {address!r}. "
                f"Must be an integer in range 0x00-0x7F (0-127)."
            )

    @abstractmethod
    def config(
        self,
        frequency_hz: int = 100_000,
        pull_ups: Optional[bool] = None,
    ) -> None:
        """
        Configure I2C bus parameters.

        Args:
            frequency_hz: I2C clock frequency in Hz (e.g., 100_000 for 100kHz standard mode,
                          400_000 for 400kHz fast mode)
            pull_ups: Enable/disable internal pull-ups (Aardvark only, ignored by LabJack)
        """
        pass

    @abstractmethod
    def scan(
        self,
        start_addr: int = 0x08,
        end_addr: int = 0x77,
    ) -> List[int]:
        """
        Scan I2C bus for connected devices.

        Probes each address in the range and returns those that ACK.

        Args:
            start_addr: First 7-bit address to probe (default 0x08)
            end_addr: Last 7-bit address to probe (default 0x77)

        Returns:
            List of 7-bit addresses that responded with ACK
        """
        pass

    @abstractmethod
    def read(
        self,
        address: int,
        num_bytes: int,
    ) -> List[int]:
        """
        Read bytes from an I2C device.

        Args:
            address: 7-bit I2C device address (0x00-0x7F)
            num_bytes: Number of bytes to read

        Returns:
            List of received bytes as integers
        """
        pass

    @abstractmethod
    def write(
        self,
        address: int,
        data: List[int],
    ) -> None:
        """
        Write bytes to an I2C device.

        Args:
            address: 7-bit I2C device address (0x00-0x7F)
            data: List of bytes to write
        """
        pass

    @abstractmethod
    def write_read(
        self,
        address: int,
        data: List[int],
        num_bytes: int,
    ) -> List[int]:
        """
        Write then read in a single I2C transaction (repeated start).

        Common pattern: write register address, then read register value
        without releasing the bus between write and read.

        Args:
            address: 7-bit I2C device address (0x00-0x7F)
            data: List of bytes to write before reading
            num_bytes: Number of bytes to read after writing

        Returns:
            List of received bytes as integers
        """
        pass
