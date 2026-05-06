# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Electronic Load module for Lager."""

from .eload_net import (
    ELoadNet,
    ELoadBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
)
from .dispatcher import (
    set_constant_current,
    get_constant_current,
    set_constant_voltage,
    get_constant_voltage,
    set_constant_resistance,
    get_constant_resistance,
    set_constant_power,
    get_constant_power,
    get_state,
)

__all__ = [
    'ELoadNet',
    'ELoadBackendError',
    'LibraryMissingError',
    'DeviceNotFoundError',
    'set_constant_current',
    'get_constant_current',
    'set_constant_voltage',
    'get_constant_voltage',
    'set_constant_resistance',
    'get_constant_resistance',
    'set_constant_power',
    'get_constant_power',
    'get_state',
]
