# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Solar/PV simulator module for Lager box.

This module provides solar/photovoltaic simulator control functionality.
"""

from __future__ import annotations

# Import base classes and exceptions from solar_net
from .solar_net import (
    SolarNet,
    SolarBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
    DeviceLockError,
)

# Import concrete implementations
from .ea import EA

# Import dispatcher functions for top-level access
from .dispatcher import (
    set_to_solar_mode,
    stop_solar_mode,
    irradiance,
    mpp_current,
    mpp_voltage,
    resistance,
    temperature,
    voc,
)

__all__ = [
    # Base classes and exceptions
    "SolarNet",
    "SolarBackendError",
    "LibraryMissingError",
    "DeviceNotFoundError",
    "DeviceLockError",
    # Concrete implementations
    "EA",
    # Dispatcher functions
    "set_to_solar_mode",
    "stop_solar_mode",
    "irradiance",
    "mpp_current",
    "mpp_voltage",
    "resistance",
    "temperature",
    "voc",
]
