# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
DAC module for digital-to-analog conversion via LabJack and USB-202 devices.

Provides abstract DAC interface and hardware-specific implementations.

Example usage:
    # High-level API
    from lager.io.dac import read, write
    current_voltage = read('my-dac-net')
    write('my-dac-net', 2.5)  # Set to 2.5V

    # Class-based API
    from lager.io.dac import LabJackDAC
    dac = LabJackDAC('my-dac-net', pin=0)
    dac.output(2.5)
"""

from __future__ import annotations

import importlib
from types import ModuleType

from .dac_net import DACBase, UnsupportedInstrumentError
from .labjack_t7 import LabJackDAC
from .usb202 import USB202DAC

__all__ = [
    "DACBase",
    "UnsupportedInstrumentError",
    "LabJackDAC",
    "USB202DAC",
    "read",
    "write",
]


_dispatcher: ModuleType | None = None


def _load_dispatcher() -> ModuleType:
    """Import lager.io.dac.dispatcher exactly once (lazy singleton)."""
    global _dispatcher  # pylint: disable=global-statement
    if _dispatcher is None:
        _dispatcher = importlib.import_module("lager.io.dac.dispatcher")
    return _dispatcher


def read(net_name: str) -> float:
    """
    Read the current voltage output from a DAC.

    Args:
        net_name: Name of the DAC net to read

    Returns:
        Current voltage output in volts as a float

    Example:
        voltage = dac.read('my-dac-net')
        print(f"Current DAC output: {voltage:.3f}V")
    """
    dispatcher = _load_dispatcher()
    return dispatcher.read_voltage(net_name)


def write(net_name: str, voltage: float) -> None:
    """
    Set the output voltage of a DAC.

    Args:
        net_name: Name of the DAC net to set
        voltage: Desired output voltage in volts

    Example:
        dac.write('reference-voltage', 2.5)  # Set to 2.5V
    """
    dispatcher = _load_dispatcher()
    dispatcher.write_voltage(net_name, voltage)
