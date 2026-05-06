# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Thermocouple module for temperature measurement.

Provides interfaces for reading temperature from thermocouple sensors.
Currently supports Phidget thermocouple sensors.
"""

from .thermocouple_net import ThermocoupleBase
from .phidget import PhidgetThermocouple
from .dispatcher import ThermocoupleDispatcher, read
from lager.exceptions import ThermocoupleBackendError

__all__ = [
    'ThermocoupleBase',
    'ThermocoupleBackendError',
    'PhidgetThermocouple',
    'ThermocoupleDispatcher',
    'read',
]
