# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Communication Commands Package

This package groups communication-related CLI commands:
- uart: UART serial port connection and communication
- ble: Bluetooth Low Energy scanning and device interaction
- wifi: WiFi network management and configuration
- usb: USB hub port control (enable/disable/toggle)
- spi: SPI (Serial Peripheral Interface) communication
- i2c: I2C (Inter-Integrated Circuit) communication

All commands handle serial, wireless, and USB communication with devices
connected to the Lager box.
"""

from .uart import uart
from .ble import ble
from .blufi import blufi
from .wifi import _wifi
from .usb import usb
from .spi import spi
from .i2c import i2c
from .mikrotik import mikrotik

__all__ = [
    "uart",
    "ble",
    "blufi",
    "_wifi",
    "usb",
    "spi",
    "i2c",
    "mikrotik",
]
