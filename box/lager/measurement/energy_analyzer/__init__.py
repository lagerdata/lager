# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Energy analyzer module for Joulescope JS220.

Provides energy accumulation (J/Wh, C/Ah) and statistics (mean/min/max/std)
over arbitrary durations. Only the Joulescope JS220 is supported.

Example usage:
    from lager.measurement.energy_analyzer import read_energy, read_stats

    result = read_energy("pwr", duration=10.0)
    print(f"Energy: {result['energy_j']:.3f} J")

    stats = read_stats("pwr", duration=1.0)
    print(f"Mean current: {stats['current']['mean']*1000:.2f} mA")
"""

from .energy_analyzer_net import EnergyAnalyzerBase
from .joulescope_energy import JoulescopeEnergyAnalyzer
from .dispatcher import EnergyAnalyzerDispatcher, read_energy, read_stats

__all__ = [
    'EnergyAnalyzerBase',
    'JoulescopeEnergyAnalyzer',
    'EnergyAnalyzerDispatcher',
    'read_energy',
    'read_stats',
]
