# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Measurement Commands Package

This package groups measurement-related CLI commands:
- adc: Read voltage from ADC net
- dac: Set or read DAC output voltage
- gpi: Read GPIO input state
- gpo: Set GPIO output level
- scope: Control oscilloscope settings
- logic: Control logic analyzer channels and triggers
- thermocouple: Read thermocouple temperature
- watt: Read power from watt meter net

All commands use the consolidated net_helpers module from cli.core for common
functionality including box resolution, net validation, and display formatting.
"""

from .adc import adc
from .dac import dac
from .gpi import gpi
from .gpo import gpo
from .scope import scope
from .logic import logic
from .thermocouple import thermocouple
from .watt import watt

__all__ = [
    "adc",
    "dac",
    "gpi",
    "gpo",
    "scope",
    "logic",
    "thermocouple",
    "watt",
]
