# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Watt meter interface for power measurement.

This module provides interfaces for measuring power consumption using
various watt meter hardware (e.g., Yoctopuce Yocto-Watt).

Example usage:
    from lager.measurement.watt import read

    # Read power from a saved net
    power = read("my_watt_net")
    print(f"Current power: {power} W")

    # Or use YoctoWatt directly
    from lager.measurement.watt import YoctoWatt
    watt = YoctoWatt(name="power_monitor", pin="1", location="usb")
    power = watt.read()
"""

from .watt_net import WattMeterBase, WattMeterBackendError, UnsupportedInstrumentError
from .yocto_watt import YoctoWatt
from .joulescope_js220 import JoulescopeJS220
from .dispatcher import WattMeterDispatcher, read

# Use centralized exception from lager.exceptions
from lager.exceptions import WattBackendError

__all__ = [
    'WattMeterBase',
    'WattMeterBackendError',
    'WattBackendError',
    'UnsupportedInstrumentError',
    'YoctoWatt',
    'JoulescopeJS220',
    'WattMeterDispatcher',
    'read',
]
