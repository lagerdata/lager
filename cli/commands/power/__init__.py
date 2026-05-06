# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Power-related CLI commands.

This package contains commands for controlling power equipment:
- supply: Power supply voltage and current control
- battery: Battery simulator control
- solar: Solar simulator control
- eload: Electronic load control

All commands use consolidated helpers from cli.core.net_helpers
to eliminate code duplication.
"""

from .supply import supply
from .battery import battery
from .solar import solar
from .eload import eload

__all__ = [
    "supply",
    "battery",
    "solar",
    "eload",
]
