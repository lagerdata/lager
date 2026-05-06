# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
I2C module for Lager box.
Provides drivers for I2C (Inter-Integrated Circuit) communication.

This module is part of the protocols package and handles I2C
communication with devices via LabJack T7 and Aardvark adapters.

Usage:
    # Object-based API (recommended)
    from lager import Net, NetType
    i2c = Net.get('MY_I2C_NET', NetType.I2C)
    i2c.config(frequency_hz=400_000)
    devices = i2c.scan()
    data = i2c.read(address=0x48, num_bytes=2)

    # Direct dispatcher functions
    from lager.protocols.i2c import config, scan, read, write, transfer
    config('i2c1', frequency_hz=400_000)
    scan('i2c1')
    read('i2c1', address=0x48, num_bytes=2)
"""
from .dispatcher import (
    config,
    scan,
    read,
    write,
    transfer,
    _resolve_net_and_driver,
)
from .i2c_base import I2CBase
from .i2c_net import I2CNet
from .labjack_i2c import LabJackI2C
from .aardvark_i2c import AardvarkI2C
from .ft232h_i2c import FT232HI2C
from lager.exceptions import I2CBackendError

__all__ = [
    # Object-based API
    'I2CNet',
    # Dispatcher functions
    'config',
    'scan',
    'read',
    'write',
    'transfer',
    # Classes
    'I2CBase',
    'LabJackI2C',
    'AardvarkI2C',
    'FT232HI2C',
    # Exceptions
    'I2CBackendError',
    # Internal
    '_resolve_net_and_driver',
]
