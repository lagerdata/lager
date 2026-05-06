# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
SPI module for Lager box.
Provides drivers for SPI (Serial Peripheral Interface) communication.

This module is part of the protocols package and handles SPI
communication with devices via LabJack T7 (and future support for
Aardvark and FTDI adapters).

Usage:
    # Object-based API (recommended)
    from lager import Net, NetType
    spi = Net.get('MY_SPI_NET', NetType.SPI)
    spi.config(mode=0, frequency_hz=1_000_000)
    response = spi.read_write([0x9F, 0x00, 0x00, 0x00])

    # Direct dispatcher functions
    from lager.protocols.spi import config, read, read_write
    config('spi1', mode=0)
    read('spi1', n_words=4)
"""
from .dispatcher import (
    config,
    read,
    read_write,
    transfer,
    _resolve_net_and_driver,
)
from .spi_base import SPIBase
from .spi_net import SPINet
from .labjack_spi import LabJackSPI
from .aardvark_spi import AardvarkSPI
from .ft232h_spi import FT232HSPI
from lager.exceptions import SPIBackendError

__all__ = [
    # Object-based API
    'SPINet',
    # Dispatcher functions
    'config',
    'read',
    'read_write',
    'transfer',
    # Classes
    'SPIBase',
    'LabJackSPI',
    'AardvarkSPI',
    'FT232HSPI',
    # Exceptions
    'SPIBackendError',
    # Internal
    '_resolve_net_and_driver',
]
