# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
GPIO module for hardware GPIO control via LabJack and USB-202 devices.

Provides abstract GPIO interface and hardware-specific implementations.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Callable, Any

from .gpio_net import GPIOBase, UnsupportedInstrumentError
from .labjack_t7 import LabJackGPIO
from .usb202 import USB202GPIO
from .ft232h_gpio import FT232HGPIO
from .aardvark_gpio import AardvarkGPIO

__all__ = [
    "GPIOBase",
    "UnsupportedInstrumentError",
    "LabJackGPIO",
    "USB202GPIO",
    "FT232HGPIO",
    "AardvarkGPIO",
    "read",
    "write",
    "gpi",
    "gpo",
    "wait_for_level",
]


_dispatcher: ModuleType | None = None


def _load_dispatcher() -> ModuleType:
    """Import lager.io.gpio.dispatcher exactly once (lazy singleton)."""
    global _dispatcher  # pylint: disable=global-statement
    if _dispatcher is None:
        _dispatcher = importlib.import_module("lager.io.gpio.dispatcher")
    return _dispatcher


def read(net_name: str) -> int:
    """
    Read the current state of a GPIO input pin.

    Args:
        net_name: Name of the GPIO net to read

    Returns:
        0 for LOW, 1 for HIGH

    Example:
        value = gpio.read('home-sensor')
        if value == 1:
            print("Sensor triggered!")
    """
    dispatcher = _load_dispatcher()
    return dispatcher.gpi(net_name)


def write(net_name: str, level: int | str) -> None:
    """
    Set the output state of a GPIO pin.

    Args:
        net_name: Name of the GPIO net to set
        level: Output level - accepts:
               - int: 0 = LOW, non-zero = HIGH
               - str: "0"/"low"/"off" = LOW, "1"/"high"/"on" = HIGH

    Example:
        gpio.write('led-enable', 1)       # Turn on
        gpio.write('motor-enable', 'high') # Turn on
        gpio.write('led-enable', 0)        # Turn off
        gpio.write('motor-enable', 'off')  # Turn off
    """
    dispatcher = _load_dispatcher()
    dispatcher.gpo(net_name, str(level))


# Aliases matching CLI command names
gpi = read
gpo = write


def wait_for_level(net_name: str, level, timeout: float | None = None, **kwargs) -> float:
    """
    Block until a GPIO pin reaches the target level.

    Args:
        net_name: Name of the GPIO net.
        level: Target level (0/1, "high"/"low").
        timeout: Maximum seconds to wait (None = forever).
        **kwargs: Driver-specific options (scan_rate, scans_per_read,
                  poll_interval).

    Returns:
        Elapsed time in seconds.
    """
    dispatcher = _load_dispatcher()
    return dispatcher.wait_for_level(net_name, level, timeout=timeout, **kwargs)
