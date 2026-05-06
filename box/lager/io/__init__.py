# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
I/O module for LabJack-based analog and digital I/O operations.

This package groups related I/O modules that share the LabJack T7 hardware backend:
- adc: Analog-to-digital conversion (reading voltages)
- dac: Digital-to-analog conversion (setting voltages)
- gpio: General purpose input/output (digital read/write)

All modules support LabJack T7 and MCC USB-202 devices.

Example usage:
    # Import submodules directly
    from lager.io import adc, dac, gpio

    # Read an ADC value
    voltage = adc.read('my-analog-net')

    # Set a DAC voltage
    dac.write('my-dac-net', 2.5)

    # Read/write GPIO
    state = gpio.read('my-input-pin')
    gpio.write('my-output-pin', 1)

    # Access classes directly
    from lager.io.adc import LabJackADC, ADCBase
    from lager.io.dac import LabJackDAC, DACBase
    from lager.io.gpio import LabJackGPIO, GPIOBase

    # Or import convenience functions directly (prefixed to avoid collisions)
    from lager.io import adc_read, dac_write, gpio_read, gpio_write

Note:
    Legacy imports (lager.adc, lager.dac, lager.gpio) have been removed.
    Use the canonical paths above instead.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING

# Lazy module loading to avoid circular imports and handle incomplete migrations
_adc: ModuleType | None = None
_dac: ModuleType | None = None
_gpio: ModuleType | None = None


def _load_adc() -> ModuleType:
    """Lazily load the ADC module."""
    global _adc  # pylint: disable=global-statement
    if _adc is None:
        _adc = importlib.import_module("lager.io.adc")
    return _adc


def _load_dac() -> ModuleType:
    """Lazily load the DAC module."""
    global _dac  # pylint: disable=global-statement
    if _dac is None:
        _dac = importlib.import_module("lager.io.dac")
    return _dac


def _load_gpio() -> ModuleType:
    """Lazily load the GPIO module."""
    global _gpio  # pylint: disable=global-statement
    if _gpio is None:
        _gpio = importlib.import_module("lager.io.gpio")
    return _gpio


# Re-export ADC public API (prefixed to avoid name collisions)
def adc_read(net_name: str) -> float:
    """
    Read the voltage from an ADC input.

    Args:
        net_name: Name of the ADC net to read

    Returns:
        Voltage reading in volts as a float
    """
    return _load_adc().read(net_name)


# Re-export DAC public API (prefixed to avoid name collisions)
def dac_read(net_name: str) -> float:
    """
    Read the current voltage output from a DAC.

    Args:
        net_name: Name of the DAC net to read

    Returns:
        Current voltage output in volts as a float
    """
    return _load_dac().read(net_name)


def dac_write(net_name: str, voltage: float) -> None:
    """
    Set the output voltage of a DAC.

    Args:
        net_name: Name of the DAC net to set
        voltage: Desired output voltage in volts
    """
    return _load_dac().write(net_name, voltage)


# Re-export GPIO public API (prefixed to avoid name collisions)
def gpio_read(net_name: str) -> int:
    """
    Read the current state of a GPIO input pin.

    Args:
        net_name: Name of the GPIO net to read

    Returns:
        0 for LOW, 1 for HIGH
    """
    return _load_gpio().read(net_name)


def gpio_write(net_name: str, level) -> None:
    """
    Set the output state of a GPIO pin.

    Args:
        net_name: Name of the GPIO net to set
        level: Output level (0/1, "low"/"high", "off"/"on")
    """
    return _load_gpio().write(net_name, level)


# Provide module-level access via __getattr__ for lazy loading
def __getattr__(name: str):
    """Lazy load submodules and classes on first access."""
    # Submodule access
    if name == "adc":
        return _load_adc()
    elif name == "dac":
        return _load_dac()
    elif name == "gpio":
        return _load_gpio()
    # ADC class access
    elif name == "ADCBase":
        return _load_adc().ADCBase
    elif name == "LabJackADC":
        return _load_adc().LabJackADC
    elif name == "USB202ADC":
        return _load_adc().USB202ADC
    # DAC class access
    elif name == "DACBase":
        return _load_dac().DACBase
    elif name == "LabJackDAC":
        return _load_dac().LabJackDAC
    elif name == "USB202DAC":
        return _load_dac().USB202DAC
    # GPIO class access
    elif name == "GPIOBase":
        return _load_gpio().GPIOBase
    elif name == "LabJackGPIO":
        return _load_gpio().LabJackGPIO
    elif name == "USB202GPIO":
        return _load_gpio().USB202GPIO
    # GPIO aliases
    elif name == "gpi":
        return _load_gpio().gpi
    elif name == "gpo":
        return _load_gpio().gpo
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    """List available submodules and exports."""
    return [
        # Submodules
        "adc",
        "dac",
        "gpio",
        # ADC exports
        "ADCBase",
        "LabJackADC",
        "USB202ADC",
        # DAC exports
        "DACBase",
        "LabJackDAC",
        "USB202DAC",
        # GPIO exports
        "GPIOBase",
        "LabJackGPIO",
        "USB202GPIO",
        # Prefixed convenience functions
        "adc_read",
        "dac_read",
        "dac_write",
        "gpio_read",
        "gpio_write",
        # GPIO aliases
        "gpi",
        "gpo",
    ]


# Type hints for IDE support
if TYPE_CHECKING:
    from lager.io.adc import ADCBase, LabJackADC, USB202ADC
    from lager.io.dac import DACBase, LabJackDAC, USB202DAC
    from lager.io.gpio import GPIOBase, LabJackGPIO, USB202GPIO, gpi, gpo


__all__ = [
    # Submodules
    "adc",
    "dac",
    "gpio",
    # ADC exports
    "ADCBase",
    "LabJackADC",
    "USB202ADC",
    # DAC exports
    "DACBase",
    "LabJackDAC",
    "USB202DAC",
    # GPIO exports
    "GPIOBase",
    "LabJackGPIO",
    "USB202GPIO",
    # Prefixed convenience functions
    "adc_read",
    "dac_read",
    "dac_write",
    "gpio_read",
    "gpio_write",
    # GPIO aliases
    "gpi",
    "gpo",
]
