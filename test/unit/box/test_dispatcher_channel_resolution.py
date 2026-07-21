# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for dispatchers.helpers.resolve_channel.

v0.32.0 regression (hardware-confirmed on MASTER): the base resolver was
int()-only, so every adc/dac net with a named channel — which is how the
scanner saves them (LabJack "AIN0"/"DAC0", MCC USB-202 "CH0") — failed with
"Invalid channel pin 'AIN0'". Named pins now pass through as strings, the
behavior the gpio dispatcher's override always had and the drivers expect.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


for _dep in ['pyvisa', 'serial', 'serial.tools', 'serial.tools.list_ports']:
    parts = _dep.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)

sys.modules.setdefault('simplejson', sys.modules['json'])

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.dispatchers import helpers  # noqa: E402


class ChannelError(Exception):
    pass


class ResolveChannelTest(unittest.TestCase):
    def test_numeric_string_pin_resolves_to_int(self):
        self.assertEqual(
            helpers.resolve_channel({'pin': '3'}, 'adc1', ChannelError), 3)

    def test_int_pin_passes_through(self):
        self.assertEqual(
            helpers.resolve_channel({'pin': 7}, 'gpio7', ChannelError), 7)

    def test_named_pin_passes_through_as_string(self):
        for named in ('AIN0', 'CH3', 'DAC0', 'FIO4', 'DIO7'):
            self.assertEqual(
                helpers.resolve_channel({'pin': named}, 'net1', ChannelError),
                named)

    def test_mapping_pin_preferred_over_top_level(self):
        rec = {
            'pin': 'AIN0',
            'mappings': [
                {'net': 'adc2', 'pin': 'AIN1'},
                {'net': 'adc3', 'pin': '4'},
            ],
        }
        self.assertEqual(
            helpers.resolve_channel(rec, 'adc2', ChannelError), 'AIN1')
        self.assertEqual(
            helpers.resolve_channel(rec, 'adc3', ChannelError), 4)
        # No matching mapping -> top-level pin.
        self.assertEqual(
            helpers.resolve_channel(rec, 'adc9', ChannelError), 'AIN0')

    def test_missing_pin_raises(self):
        with self.assertRaises(ChannelError):
            helpers.resolve_channel({}, 'adc1', ChannelError)


if __name__ == '__main__':
    unittest.main()
