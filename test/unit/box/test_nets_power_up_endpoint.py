# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for a gpio net's declared power-up level.

``PUT /nets/<name>/power-up`` records the level (merging, like
``/safety-limits``); ``POST /nets/power-up`` checks or writes each LabJack's
boot config from the declared nets, one hardware_service call per LabJack,
locked on the same device identity as ``/invoke``. The whole-record
``PUT /nets/<name>`` validates the field too, so no save path stores a level
the apply cannot honor.
"""

import json
import os
import sys
import tempfile
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
    'bleak', 'picoscope', 'brainstem',
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
from lager.cache import get_nets_cache  # noqa: E402
from lager.http_handlers import nets_handler  # noqa: E402
from lager.nets import net as net_module  # noqa: E402

T7 = "USB0::0x0CD5::0x0007::470000001::INSTR"
U3 = "USB0::0x0CD5::0x0003::320000001::INSTR"


def _saved_nets():
    return [
        {"name": "PUMP", "role": "gpio", "instrument": "LabJack_T7", "address": T7, "pin": "FIO3"},
        {"name": "BLOW", "role": "gpio", "instrument": "LabJack_T7", "address": T7, "pin": "FIO2"},
        {"name": "SENSE", "role": "gpio", "instrument": "LabJack_T7", "address": T7, "pin": "FIO0"},
        {"name": "VALVE", "role": "gpio", "instrument": "LabJack_U3", "address": U3, "pin": "EIO2"},
        {"name": "AIN", "role": "adc", "instrument": "LabJack_T7", "address": T7, "pin": 0},
        {"name": "FT", "role": "gpio", "instrument": "FTDI_FT232H",
         "address": "USB0::0x0403::0x6014::FT1::INSTR", "pin": 4},
    ]


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'saved_nets.json')
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(_saved_nets(), f)
        self._orig_write_path = net_module.LOCAL_NETS_PATH
        net_module.LOCAL_NETS_PATH = self.path
        self.cache = get_nets_cache()
        self._orig_cache_path = self.cache._path
        self.cache._path = self.path
        self.cache.invalidate()
        app = Flask(__name__)
        nets_handler.register_nets_routes(app)
        self.client = app.test_client()

    def tearDown(self):
        net_module.LOCAL_NETS_PATH = self._orig_write_path
        self.cache._path = self._orig_cache_path
        self.cache.invalidate()
        self.tmp.cleanup()

    def record(self, name):
        with open(self.path, encoding='utf-8') as f:
            return next(n for n in json.load(f) if n.get('name') == name)


class SetPowerUpTests(_Fixture):
    def test_sets_the_level_and_leaves_every_other_field_alone(self):
        before = self.record('PUMP')
        res = self.client.put('/nets/PUMP/power-up', json={"power_up": "LOW"})
        self.assertEqual(res.status_code, 200)
        after = self.record('PUMP')
        self.assertEqual(after['power_up'], 'low')
        self.assertEqual({k: v for k, v in after.items() if k != 'power_up'}, before)

    def test_null_clears_it(self):
        self.client.put('/nets/PUMP/power-up', json={"power_up": "high"})
        res = self.client.put('/nets/PUMP/power-up', json={"power_up": None})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('power_up', self.record('PUMP'))

    def test_refuses_non_gpio_and_non_labjack_nets(self):
        for name in ('AIN', 'FT'):
            res = self.client.put(f'/nets/{name}/power-up', json={"power_up": "low"})
            self.assertEqual(res.status_code, 400, name)
            self.assertNotIn('power_up', self.record(name))

    def test_refuses_a_level_that_is_not_low_or_high(self):
        for value in ("input", "on", 0, ""):
            res = self.client.put('/nets/PUMP/power-up', json={"power_up": value})
            self.assertEqual(res.status_code, 400, value)

    def test_missing_key_and_unknown_net(self):
        self.assertEqual(self.client.put('/nets/PUMP/power-up', json={}).status_code, 400)
        self.assertEqual(
            self.client.put('/nets/NOPE/power-up', json={"power_up": "low"}).status_code, 404)

    def test_whole_record_put_validates_the_field(self):
        rec = dict(self.record('FT'), power_up='low')
        self.assertEqual(self.client.put('/nets/FT', json=rec).status_code, 400)
        rec = dict(self.record('PUMP'), power_up='High')
        self.assertEqual(self.client.put('/nets/PUMP', json=rec).status_code, 200)
        self.assertEqual(self.record('PUMP')['power_up'], 'high')


class ApplyTests(_Fixture):
    def _post(self, body, run):
        calls = []

        class FakeDevice:
            def __init__(self, name, net_info, timeout=None):
                self.name, self.net_info = name, net_info

            def run(self, **kw):
                calls.append((self.name, self.net_info, kw))
                return run(self.net_info, kw)

        with patch('lager.nets.device.Device', FakeDevice):
            res = self.client.post('/nets/power-up', json=body)
        return res, calls

    def test_one_call_per_labjack_with_only_its_declared_nets(self):
        self.client.put('/nets/PUMP/power-up', json={"power_up": "low"})
        self.client.put('/nets/BLOW/power-up', json={"power_up": "low"})
        self.client.put('/nets/VALVE/power-up', json={"power_up": "low"})
        res, calls = self._post({"mode": "plan"},
                                lambda info, kw: {"model": "X", "in_sync": True, "pins": []})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(calls), 2)
        by_device = {c[1]['device_id']: sorted(n['name'] for n in c[2]['nets']) for c in calls}
        self.assertIn(['BLOW', 'PUMP'], by_device.values())
        self.assertIn(['VALVE'], by_device.values())
        for name, info, kw in calls:
            self.assertEqual(name, 'power_up_hs')
            self.assertEqual(kw['mode'], 'plan')
        data = res.get_json()
        self.assertEqual(data['undeclared'], ['SENSE'])
        self.assertTrue(data['in_sync'])

    def test_the_lock_identity_matches_invoke(self):
        from lager.http_handlers.net_command import _physical_device_id
        self.client.put('/nets/PUMP/power-up', json={"power_up": "low"})
        _, calls = self._post({"mode": "plan"},
                              lambda info, kw: {"in_sync": True, "pins": []})
        self.assertEqual(calls[0][1]['device_id'],
                         _physical_device_id('gpio', 'LabJack_T7', self.record('PUMP')))

    def test_a_device_error_is_reported_per_device(self):
        from lager.nets.device import DeviceError
        self.client.put('/nets/PUMP/power-up', json={"power_up": "low"})

        def boom(info, kw):
            raise DeviceError({"error": "pins FIO0 are outputs"})
        res, _ = self._post({"mode": "apply"}, boom)
        data = res.get_json()
        self.assertEqual(data['devices'][0]['error'], 'pins FIO0 are outputs')
        # A failed entry still names its family, so the CLI can title it.
        self.assertEqual(data['devices'][0]['backend'], 'labjack')
        self.assertFalse(data['in_sync'])

    def test_apply_passes_allow_undeclared(self):
        self.client.put('/nets/PUMP/power-up', json={"power_up": "low"})
        _, calls = self._post({"mode": "apply", "allow_undeclared": True},
                              lambda info, kw: {"in_sync": True, "pins": []})
        self.assertEqual(calls[0][2]['mode'], 'apply')
        self.assertTrue(calls[0][2]['allow_undeclared'])

    def test_unknown_mode_is_400(self):
        res = self.client.post('/nets/power-up', json={"mode": "write"})
        self.assertEqual(res.status_code, 400)

    def test_nothing_declared_reports_no_devices(self):
        res, calls = self._post({}, lambda info, kw: {})
        self.assertEqual(calls, [])
        self.assertEqual(res.get_json()['devices'], [])


if __name__ == '__main__':
    unittest.main()
