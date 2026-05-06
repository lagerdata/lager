# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Lager Box Python API.

This is the main entry point for the Lager hardware control library.
It provides access to all hardware control functionality organized into
logical groups:

Module Groups:
    lager.power     - Power equipment (supply, battery, solar, eload)
    lager.io        - I/O devices (adc, dac, gpio)
    lager.measurement - Measurement instruments (scope, thermocouple, watt)
    lager.protocols - Communication protocols (uart, ble, wifi)
    lager.automation - Automation hardware (arm, usb_hub, webcam)
    lager.debug     - Embedded debugging (J-Link, pyOCD, GDB)

Core Classes:
    Net, NetType    - PCB net abstraction for test points
    Hexfile, Binfile - Firmware file handling
    Interface, Transport - Debug interface configuration

Example usage:
    # High-level API
    from lager import Net, NetType
    from lager.power.supply import voltage, enable

    # Set power supply to 3.3V
    voltage('VBAT', 3.3)
    enable('VBAT')

    # Read ADC
    from lager.io.adc import read
    value = read('my-adc-net')
"""

from __future__ import annotations

# =============================================================================
# Core Module Exports
# =============================================================================
from .core import (
    Interface,
    Transport,
    OutputEncoders,
    output,
    lager_excepthook,
    restore_excepthook,
    install_excepthook,
    Hexfile,
    Binfile,
    read_adc,
    get_available_instruments,
    get_saved_nets,
    LAGER_HOST,
)

# =============================================================================
# Net Abstraction (test points/signals)
# =============================================================================
from .nets.net import Net, NetType, InvalidNetError, SetupFunctionRequiredError

# =============================================================================
# Exception Classes (from automation.usb_hub)
# =============================================================================
from .automation.usb_hub import (
    USBBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
    PortStateError,
)

# =============================================================================
# __all__ Export List
# =============================================================================
__all__ = [
    # Core enums and classes
    "Interface",
    "Transport",
    "OutputEncoders",
    "Hexfile",
    "Binfile",
    # Core functions
    "output",
    "lager_excepthook",
    "restore_excepthook",
    "install_excepthook",
    "read_adc",
    "get_available_instruments",
    "get_saved_nets",
    # Core constants
    "LAGER_HOST",
    # PCB Net abstraction
    "Net",
    "NetType",
    "InvalidNetError",
    "SetupFunctionRequiredError",
    # Exception classes (backward compatibility)
    "USBBackendError",
    "LibraryMissingError",
    "DeviceNotFoundError",
    "PortStateError",
]