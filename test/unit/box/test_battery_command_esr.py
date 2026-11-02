# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the `set_esr` action of POST /battery/command
(box/lager/http_handlers/battery.py).

The 2281S can only set a series offset on top of the active battery model, so
the action reports what the instrument answered, not what was asked for.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted: str) -> None:
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


_HARDWARE_STUBS = [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'pigpio', 'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
    'flask_socketio',
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from flask import Flask  # noqa: E402
from lager.exceptions import BatteryBackendError  # noqa: E402
from lager.http_handlers import battery as battery_handler  # noqa: E402
from lager.nets.device import DeviceError  # noqa: E402


class BatteryEsrCommandTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        battery_handler.register_battery_routes(app)
        self.client = app.test_client()
        self.battery = MagicMock()
        self.battery._safe_query.return_value = '0.4'

        self._orig_resolve_proxy = battery_handler._resolve_battery_proxy
        battery_handler._resolve_battery_proxy = lambda netname: (self.battery, 1)

        # Skip the cross-role conflict lookup (no saved nets in tests).
        def _no_net(netname, role, error_class):
            raise BatteryBackendError('no saved nets in test')
        import lager.dispatchers.helpers as helpers
        self._helpers = helpers
        self._orig_resolve_net = helpers.resolve_net_proxy
        helpers.resolve_net_proxy = _no_net

    def tearDown(self):
        battery_handler._resolve_battery_proxy = self._orig_resolve_proxy
        self._helpers.resolve_net_proxy = self._orig_resolve_net

    def _post(self, params=None):
        return self.client.post('/battery/command', json={
            'netname': 'batt1',
            'action': 'set_esr',
            'params': params or {},
        })

    def test_set_writes_the_offset_and_reports_the_instrument(self):
        # The instrument clamped 0.5 to 0.4.
        resp = self._post({'value': 0.5})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['value'], 0.4)
        self.assertIn('set to 0.4 ohm', body['message'])
        self.battery.set_esr.assert_called_once_with(0.5)
        self.battery._safe_query.assert_called_once_with(':BATT:SIM:RES:OFFS?', '0')

    def test_no_value_reads_the_offset_without_writing(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body['value'], 0.4)
        self.assertEqual(body['message'], 'ESR offset: 0.4 ohm')
        self.battery.set_esr.assert_not_called()

    def test_zero_is_a_set_not_a_read(self):
        self.battery._safe_query.return_value = '0'
        resp = self._post({'value': 0})
        self.assertEqual(resp.status_code, 200)
        self.battery.set_esr.assert_called_once_with(0)

    def test_out_of_range_comes_back_as_the_driver_said_it(self):
        self.battery.set_esr.side_effect = DeviceError(
            {'error': 'Function call failed: ESR offset out of range (must be -100 to 100 ohm)'})
        resp = self._post({'value': 500})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            'success': False,
            'error': 'ESR offset out of range (must be -100 to 100 ohm)',
        })


if __name__ == '__main__':
    unittest.main()
