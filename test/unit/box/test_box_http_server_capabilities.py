# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the box_http_server /status capabilities block.

Regression: the advertised `netCommand` capability must reflect whether the
POST /net/command route actually registered (`_has_net_command`), not be
hardcoded True. If the handler import fails the route is never registered, but
a hardcoded True still tells the control plane the box serves /net/command —
so it routes there and the box 404s ("The requested endpoint does not
exist").

The box package has hardware-only dependencies (pyvisa, usb, labjack, …) that
only exist inside the Docker container. We stub them in sys.modules before
import so these tests run on any developer machine.
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

from lager import box_http_server  # noqa: E402


class StatusCapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.client = box_http_server.app.test_client()
        self._orig_has_net_command = box_http_server._has_net_command

    def tearDown(self):
        box_http_server._has_net_command = self._orig_has_net_command

    def _capabilities(self):
        resp = self.client.get('/status')
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()['capabilities']

    def test_advertises_netcommand_when_route_registered(self):
        box_http_server._has_net_command = True
        self.assertIs(self._capabilities()['netCommand'], True)

    def test_does_not_advertise_netcommand_when_handler_unavailable(self):
        # Import failed → route never registered → must not claim the capability.
        box_http_server._has_net_command = False
        self.assertIs(self._capabilities()['netCommand'], False)


if __name__ == '__main__':
    unittest.main()
