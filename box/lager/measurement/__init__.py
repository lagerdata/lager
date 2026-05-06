# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Measurement module group for Lager.

This group contains modules for measuring physical quantities:
- scope: Oscilloscope control (Rigol MSO5000)
- thermocouple: Temperature measurement (Phidget sensors)
- watt: Power measurement (Yoctopuce watt meters)

Example usage:
    from lager.measurement.thermocouple import read as read_temp
    temp = read_temp("my_thermocouple_net")

    from lager.measurement.watt import read as read_power
    power = read_power("my_watt_net")

    from lager.measurement.scope import RigolMso5000
    scope = RigolMso5000(address="TCPIP::<YOUR_SCOPE_IP>::INSTR")
"""

# Scope exports
from .scope import RigolMso5000, RigolMSO5000, create_device

# Thermocouple exports
from .thermocouple import (
    ThermocoupleDispatcher,
    PhidgetThermocouple,
    ThermocoupleBase,
    read as thermocouple_read,
)

# Watt meter exports
from .watt import (
    WattMeterDispatcher,
    YoctoWatt,
    WattMeterBase,
    WattMeterBackendError,
    UnsupportedInstrumentError,
    read as watt_read,
)

# Energy analyzer exports
from .energy_analyzer import (
    EnergyAnalyzerDispatcher,
    JoulescopeEnergyAnalyzer,
    EnergyAnalyzerBase,
    read_energy as energy_read,
    read_stats as stats_read,
)

# Use centralized exceptions from lager.exceptions
from lager.exceptions import ThermocoupleBackendError, WattBackendError, EnergyAnalyzerBackendError

__all__ = [
    # Scope
    'RigolMso5000',
    'RigolMSO5000',
    'create_device',
    # Thermocouple
    'ThermocoupleDispatcher',
    'PhidgetThermocouple',
    'ThermocoupleBase',
    'ThermocoupleBackendError',
    'thermocouple_read',
    # Watt meter
    'WattMeterDispatcher',
    'YoctoWatt',
    'WattMeterBase',
    'WattMeterBackendError',
    'WattBackendError',
    'UnsupportedInstrumentError',
    'watt_read',
    # Energy analyzer
    'EnergyAnalyzerDispatcher',
    'JoulescopeEnergyAnalyzer',
    'EnergyAnalyzerBase',
    'EnergyAnalyzerBackendError',
    'energy_read',
    'stats_read',
]
