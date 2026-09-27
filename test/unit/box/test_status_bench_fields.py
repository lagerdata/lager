# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""``/status`` advertises the bench manifest and carries the two new per-net
metadata fields.

A client that keeps a copy of the bench gates its ``GET /bench`` on
``capabilities.benchManifest``; a box predating the route must read as
"older box", not as "no bench". And the per-net metadata that rides along on
``/status`` must include ``dut_connection`` and ``test_hints`` with the same
always-present-empty-when-unset contract as ``purpose``, so last-write-wins
reconciliation can tell "empty" from "predates the field".
"""

import io
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


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

from lager import box_http_server  # noqa: E402


class BenchManifestCapabilityTest(unittest.TestCase):
    def setUp(self):
        self.client = box_http_server.app.test_client()

    def _capabilities(self):
        resp = self.client.get('/status')
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()['capabilities']

    def test_bench_manifest_capability_reflects_registration(self):
        orig = box_http_server._has_bench_manifest
        try:
            box_http_server._has_bench_manifest = True
            self.assertIs(self._capabilities()['benchManifest'], True)
            box_http_server._has_bench_manifest = False
            self.assertIs(self._capabilities()['benchManifest'], False)
        finally:
            box_http_server._has_bench_manifest = orig

    def test_the_route_is_registered_on_the_real_app(self):
        # The handler imports cleanly on a developer host, so the flag is
        # true and /bench is mounted; this guards against the import guard
        # silently swallowing a regression.
        self.assertTrue(box_http_server._has_bench_manifest)
        rules = {r.rule for r in box_http_server.app.url_map.iter_rules()}
        self.assertIn('/bench', rules)


class StatusNetMetadataFieldsTest(unittest.TestCase):
    """The nets block on /status carries every user-metadata field."""

    def setUp(self):
        self.client = box_http_server.app.test_client()

    def _status_with_saved_nets(self, saved_nets):
        real_open = open
        payload = json.dumps(saved_nets)

        def fake_open(path, *args, **kwargs):
            if path == '/etc/lager/saved_nets.json':
                return io.StringIO(payload)
            if str(path).startswith('/etc/lager/'):
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        with patch('builtins.open', side_effect=fake_open):
            resp = self.client.get('/status')
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()['nets']

    def test_new_fields_ride_along_and_are_present_when_unset(self):
        nets = self._status_with_saved_nets([
            {"name": "uart1", "role": "uart", "purpose": "console",
             "dut_connection": "J3 pin 4", "test_hints": ["hold nRST"]},
            {"name": "psu1", "role": "power-supply"},
        ])
        by_name = {n['name']: n for n in nets}
        self.assertEqual(by_name['uart1']['dut_connection'], 'J3 pin 4')
        self.assertEqual(by_name['uart1']['test_hints'], ['hold nRST'])
        self.assertEqual(by_name['psu1']['dut_connection'], '')
        self.assertEqual(by_name['psu1']['test_hints'], [])
        # The existing contract for the older fields is unchanged.
        self.assertEqual(by_name['psu1']['purpose'], '')
        self.assertEqual(by_name['psu1']['tags'], [])
