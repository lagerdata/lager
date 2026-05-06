# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Keithley Battery Simulator Module Alias

This module exists to allow the hardware service to import battery functionality
using the device name "keithley_battery" instead of "keithley". This avoids
import path conflicts with the power supply module (lager.supply.keithley).

All functionality is imported from the main keithley battery module.
"""

from .keithley import KeithleyBattery, Keithley, create_device

__all__ = ['KeithleyBattery', 'Keithley', 'create_device']
