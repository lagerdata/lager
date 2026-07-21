# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the supply HTTP command handler
(box/lager/http_handlers/supply.py — POST /supply/command).

Covers the v0.32.0 regressions found in hardware testing:
  - `set_mode` was not in the action chain, so `lager supply <net> set`
    always returned 400 "Unknown action: set_mode".
  - `--ocp`/`--ovp` on voltage/current were silently discarded (the handler
    read only params['value']), leaving protection limits unset.
  - clear_ocp/clear_ovp passed a channel= kwarg the EA driver doesn't
    accept; the handler now uses the uniform clear_ocp()/clear_ovp()
    driver wrappers.
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

sys.modules['simplejson'] = sys.modules['json']  # type: ignore[assignment]

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from flask import Flask  # noqa: E402
from lager.exceptions import SupplyBackendError  # noqa: E402
from lager.http_handlers import supply as supply_handler  # noqa: E402


class FakeSupply:
    """Records driver calls the way the hardware_service Device proxy would."""

    def __init__(self):
        self.calls = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def voltage(self, value=None, ocp=None, ovp=None):
        self._record('voltage', value=value, ocp=ocp, ovp=ovp)

    def current(self, value=None, ocp=None, ovp=None):
        self._record('current', value=value, ocp=ocp, ovp=ovp)

    def set_mode(self, *args, **kwargs):
        self._record('set_mode', *args, **kwargs)

    def clear_ocp(self, *args, **kwargs):
        self._record('clear_ocp', *args, **kwargs)

    def clear_ovp(self, *args, **kwargs):
        self._record('clear_ovp', *args, **kwargs)

    def get_channel_voltage(self, source=None):
        self._record('get_channel_voltage', source=source)
        return 5.0

    def get_channel_current(self, source=None):
        self._record('get_channel_current', source=source)
        return 1.0

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class SupplyCommandHandlerTest(unittest.TestCase):
    VOLTAGE_MAX = 32.0
    CURRENT_MAX = 5.0

    def setUp(self):
        app = Flask(__name__)
        supply_handler.register_supply_routes(app)
        self.client = app.test_client()
        self.supply = FakeSupply()

        self._orig_resolve_proxy = supply_handler._resolve_supply_proxy
        self._orig_resolve_net = supply_handler.resolve_net_proxy
        supply_handler._resolve_supply_proxy = lambda netname: (
            self.supply, 1, self.VOLTAGE_MAX, self.CURRENT_MAX
        )

        # Skip the cross-role conflict lookup (no saved nets in tests).
        def _no_net(netname, role, error_class):
            raise SupplyBackendError('no saved nets in test')
        supply_handler.resolve_net_proxy = _no_net

    def tearDown(self):
        supply_handler._resolve_supply_proxy = self._orig_resolve_proxy
        supply_handler.resolve_net_proxy = self._orig_resolve_net

    def _post(self, action, params=None):
        return self.client.post('/supply/command', json={
            'netname': 'supply1',
            'action': action,
            'params': params or {},
        })

    # ---- set_mode -----------------------------------------------------

    def test_set_mode_invokes_driver(self):
        resp = self._post('set_mode')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        self.assertEqual(len(self.supply.named('set_mode')), 1)

    # ---- voltage / current protections --------------------------------

    def test_voltage_forwards_ocp_and_ovp(self):
        resp = self._post('voltage', {'value': 5.0, 'ocp': 2.0, 'ovp': 6.0})
        self.assertEqual(resp.status_code, 200)
        (_, _, kwargs), = self.supply.named('voltage')
        self.assertEqual(kwargs, {'value': 5.0, 'ocp': 2.0, 'ovp': 6.0})
        message = resp.get_json()['message']
        self.assertIn('5.0V', message)
        self.assertIn('OCP 2.0A', message)
        self.assertIn('OVP 6.0V', message)

    def test_voltage_with_only_protection_applies_it(self):
        # `lager supply <net> voltage --ovp 6` (no value) must apply the
        # protection rather than falling into the read path.
        resp = self._post('voltage', {'value': None, 'ovp': 6.0})
        self.assertEqual(resp.status_code, 200)
        (_, _, kwargs), = self.supply.named('voltage')
        self.assertEqual(kwargs, {'value': None, 'ocp': None, 'ovp': 6.0})
        self.assertFalse(self.supply.named('get_channel_voltage'))

    def test_voltage_read_path_unchanged(self):
        resp = self._post('voltage')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.supply.named('voltage'))
        self.assertEqual(len(self.supply.named('get_channel_voltage')), 1)

    def test_current_forwards_ocp_and_ovp(self):
        resp = self._post('current', {'value': 1.5, 'ocp': 2.0})
        self.assertEqual(resp.status_code, 200)
        (_, _, kwargs), = self.supply.named('current')
        self.assertEqual(kwargs, {'value': 1.5, 'ocp': 2.0, 'ovp': None})

    def test_ovp_beyond_hardware_limit_rejected(self):
        resp = self._post('voltage', {'value': 5.0, 'ovp': self.VOLTAGE_MAX + 1})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(self.supply.named('voltage'))

    def test_ocp_beyond_hardware_limit_rejected(self):
        resp = self._post('current', {'value': 1.0, 'ocp': self.CURRENT_MAX + 1})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(self.supply.named('current'))

    # ---- clear_ocp / clear_ovp ----------------------------------------

    def test_clear_ocp_uses_uniform_wrapper_without_channel(self):
        resp = self._post('clear_ocp')
        self.assertEqual(resp.status_code, 200)
        (_, args, kwargs), = self.supply.named('clear_ocp')
        self.assertEqual(args, ())
        self.assertEqual(kwargs, {})

    def test_clear_ovp_uses_uniform_wrapper_without_channel(self):
        resp = self._post('clear_ovp')
        self.assertEqual(resp.status_code, 200)
        (_, args, kwargs), = self.supply.named('clear_ovp')
        self.assertEqual(args, ())
        self.assertEqual(kwargs, {})

    # ---- guardrails ---------------------------------------------------

    def test_unknown_action_still_400(self):
        resp = self._post('warp_drive')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Unknown action', resp.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
