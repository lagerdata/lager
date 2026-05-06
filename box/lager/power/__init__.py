# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Power management modules for Lager box.

This package provides unified access to all power-related hardware control:
- supply: Power supply control (voltage, current, enable/disable)
- battery: Battery simulator control (SOC, VOC, capacity, etc.)
- solar: Solar/photovoltaic simulator control (irradiance, MPP, etc.)
- eload: Electronic load control (constant current, voltage, power, resistance)

Submodule imports:
    from lager.power import supply
    from lager.power.supply import voltage, enable, disable
    from lager.power.battery.dispatcher import set_soc

Note: Legacy imports (lager.supply, lager.battery) have been removed.
    Use the canonical paths above instead.
"""

from __future__ import annotations

# Import submodules for attribute access (from lager.power import supply)
from . import supply
from . import battery
from . import solar
from . import eload

# Re-export supply functions for convenience
from .supply import (
    voltage as supply_voltage,
    current as supply_current,
    enable as supply_enable,
    disable as supply_disable,
    state as supply_state,
    set_mode as supply_set_mode,
    clear_ocp as supply_clear_ocp,
    clear_ovp as supply_clear_ovp,
)

# Re-export battery functions from dispatcher
from .battery.dispatcher import (
    set_mode as battery_set_mode,
    set_to_battery_mode,
    set_soc,
    set_voc,
    set_volt_full,
    set_volt_empty,
    set_capacity,
    set_current_limit,
    set_ovp as battery_set_ovp,
    set_ocp as battery_set_ocp,
    set_model,
    enable_battery,
    disable_battery,
    print_state as battery_print_state,
    clear as battery_clear,
    clear_ovp as battery_clear_ovp,
    clear_ocp as battery_clear_ocp,
    terminal_voltage,
    current as battery_current,
    esr,
)

# Re-export solar functions from dispatcher
from .solar.dispatcher import (
    set_to_solar_mode,
    stop_solar_mode,
    irradiance,
    mpp_current,
    mpp_voltage,
    resistance as solar_resistance,
    temperature as solar_temperature,
    voc as solar_voc,
)

# Re-export eload public API
from .eload import (
    ELoadNet,
    ELoadBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
    set_constant_current,
    get_constant_current,
    set_constant_voltage,
    get_constant_voltage,
    set_constant_resistance,
    get_constant_resistance,
    set_constant_power,
    get_constant_power,
)

__all__ = [
    # Submodules
    "supply",
    "battery",
    "solar",
    "eload",
    # Supply functions (prefixed to avoid name collisions)
    "supply_voltage",
    "supply_current",
    "supply_enable",
    "supply_disable",
    "supply_state",
    "supply_set_mode",
    "supply_clear_ocp",
    "supply_clear_ovp",
    # Battery functions
    "battery_set_mode",
    "set_to_battery_mode",
    "set_soc",
    "set_voc",
    "set_volt_full",
    "set_volt_empty",
    "set_capacity",
    "set_current_limit",
    "battery_set_ovp",
    "battery_set_ocp",
    "set_model",
    "enable_battery",
    "disable_battery",
    "battery_print_state",
    "battery_clear",
    "battery_clear_ovp",
    "battery_clear_ocp",
    "terminal_voltage",
    "battery_current",
    "esr",
    # Solar functions
    "set_to_solar_mode",
    "stop_solar_mode",
    "irradiance",
    "mpp_current",
    "mpp_voltage",
    "solar_resistance",
    "solar_temperature",
    "solar_voc",
    # ELoad exports
    "ELoadNet",
    "ELoadBackendError",
    "LibraryMissingError",
    "DeviceNotFoundError",
    "set_constant_current",
    "get_constant_current",
    "set_constant_voltage",
    "get_constant_voltage",
    "set_constant_resistance",
    "get_constant_resistance",
    "set_constant_power",
    "get_constant_power",
]
