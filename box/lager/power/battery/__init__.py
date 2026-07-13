# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Battery simulation module for Lager box.

This module provides battery simulator control functionality.
"""

from __future__ import annotations

# Import base classes and exceptions from battery_net
from .battery_net import (
    BatteryNet,
    BatteryBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
)

# Import concrete implementations
from .keithley import KeithleyBattery, Keithley, create_device

# Import dispatcher functions for top-level access
from .dispatcher import (
    set_mode,
    set_to_battery_mode,
    set,
    set_soc,
    set_voc,
    set_volt_full,
    set_volt_empty,
    set_capacity,
    set_current_limit,
    set_ovp,
    set_ocp,
    set_model,
    list_models,
    format_model_catalog,
    enable_battery,
    disable_battery,
    print_state,
    clear,
    clear_ovp,
    clear_ocp,
    terminal_voltage,
    current,
    esr,
)

__all__ = [
    # Base classes and exceptions
    "BatteryNet",
    "BatteryBackendError",
    "LibraryMissingError",
    "DeviceNotFoundError",
    # Concrete implementations
    "KeithleyBattery",
    "Keithley",
    "create_device",
    # Dispatcher functions
    "set_mode",
    "set_to_battery_mode",
    "set",
    "set_soc",
    "set_voc",
    "set_volt_full",
    "set_volt_empty",
    "set_capacity",
    "set_current_limit",
    "set_ovp",
    "set_ocp",
    "set_model",
    "list_models",
    "format_model_catalog",
    "enable_battery",
    "disable_battery",
    "print_state",
    "clear",
    "clear_ovp",
    "clear_ocp",
    "terminal_voltage",
    "current",
    "esr",
]
