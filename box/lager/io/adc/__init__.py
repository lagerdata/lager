# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
ADC module for analog-to-digital conversion via LabJack and USB-202 devices.

Provides abstract ADC interface and hardware-specific implementations.

Example usage:
    # High-level API
    from lager.io.adc import read
    voltage = read('my-adc-net')

    # Class-based API
    from lager.io.adc import LabJackADC
    adc = LabJackADC('my-adc-net', pin=0)
    voltage = adc.input()
"""

from lager.io.adc.adc_net import ADCBase, UnsupportedInstrumentError
from lager.io.adc.labjack_t7 import LabJackADC
from lager.io.adc.usb202 import USB202ADC
from lager.io.adc.dispatcher import read, voltage, _do_adc_read, ADCDispatcher

__all__ = [
    "ADCBase",
    "UnsupportedInstrumentError",
    "LabJackADC",
    "USB202ADC",
    "ADCDispatcher",
    "read",
    "voltage",
    "_do_adc_read",  # Backward compat
]
