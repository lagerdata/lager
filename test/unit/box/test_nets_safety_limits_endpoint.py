# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for PUT /nets/<name>/safety-limits.

The endpoint exists so a limit can be configured centrally instead of by
hand-editing ``saved_nets.json`` on each box. Three properties carry the
feature, and each is pinned here:

**What the route writes is what the interlock reads.** ``ContractTests`` drives
a real write through the route and then calls ``lager.safety`` against the same
file. A route that stored a well-formed record the enforcement path did not
recognise would pass every test that only inspected JSON, while leaving the
bench unprotected -- the exact failure the feature is meant to remove.

**Every record sharing the name is updated.** Limits are read back through
``NetsCache.find_by_name``, which keeps one record per name. Updating only the
first match would make enforcement depend on file order.

**A refused body changes nothing.** Validation runs before the read-modify-write,
so a partially-valid payload cannot leave half its limits applied.

``max_power`` gets its own test because it is the one field a reader would
reasonably expect to work: it is refused loudly rather than stored and ignored.
"""

import json
import os
import sys
import tempfile
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
from lager import safety  # noqa: E402
from lager.cache import get_nets_cache  # noqa: E402
from lager.http_handlers import nets_handler  # noqa: E402
from lager.nets import net as net_module  # noqa: E402


def _saved_nets():
    """Two supplies and a battery. 'vbat' deliberately appears twice."""
    return [
        {"name": "vbat", "role": "power-supply", "instrument": "Keysight_E36313A",
         "address": "USB0::0x2A8D::0x1002::MY1::INSTR", "channel": 1, "pin": 0},
        {"name": "vbat", "role": "battery", "instrument": "Keysight_E36731A",
         "address": "USB0::0x2A8D::0x1102::MY2::INSTR", "channel": 1, "pin": 0},
        {"name": "v33", "role": "power-supply", "instrument": "Keysight_E36313A",
         "address": "USB0::0x2A8D::0x1002::MY1::INSTR", "channel": 2, "pin": 0},
    ]


class _NetsFileFixture(unittest.TestCase):
    """Points both the writer and the cache at a temp saved_nets.json."""

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

        safety.reset_rate_state()

        app = Flask(__name__)
        nets_handler.register_nets_routes(app)
        self.client = app.test_client()

    def tearDown(self):
        net_module.LOCAL_NETS_PATH = self._orig_write_path
        self.cache._path = self._orig_cache_path
        self.cache.invalidate()
        safety.reset_rate_state()
        self.tmp.cleanup()

    def read_file(self):
        with open(self.path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def records_named(self, name):
        return [n for n in self.read_file() if n.get('name') == name]


class ValidationTests(unittest.TestCase):
    """_validate_safety_limits, exercised directly -- no filesystem."""

    def test_max_power_is_refused_with_a_reason(self):
        limits, error = nets_handler._validate_safety_limits({"max_power": 10.0})
        self.assertIsNone(limits)
        self.assertIn('max_power is not supported', error)
        self.assertIn('max_voltage', error)

    def test_unknown_key_is_refused(self):
        limits, error = nets_handler._validate_safety_limits({"max_temperature": 40})
        self.assertIsNone(limits)
        self.assertIn('max_temperature', error)

    def test_boolean_is_not_a_ceiling(self):
        # bool subclasses int; True would otherwise be stored as a 1.0 V ceiling.
        limits, error = nets_handler._validate_safety_limits({"max_voltage": True})
        self.assertIsNone(limits)
        self.assertIn('must be a number', error)

    def test_string_is_not_a_ceiling(self):
        limits, error = nets_handler._validate_safety_limits({"max_voltage": "5"})
        self.assertIsNone(limits)
        self.assertIn('must be a number', error)

    def test_non_positive_ceiling_is_refused(self):
        for bad in (0, -1.5):
            limits, error = nets_handler._validate_safety_limits({"max_current": bad})
            self.assertIsNone(limits, bad)
            self.assertIn('greater than zero', error)

    def test_allow_destructive_must_be_boolean(self):
        limits, error = nets_handler._validate_safety_limits({"allow_destructive": "no"})
        self.assertIsNone(limits)
        self.assertIn('must be a boolean', error)

    def test_ceilings_are_coerced_to_float(self):
        limits, error = nets_handler._validate_safety_limits(
            {"max_voltage": 5, "max_current": 1, "allow_destructive": False})
        self.assertIsNone(error)
        self.assertEqual(limits, {"max_voltage": 5.0, "max_current": 1.0,
                                  "allow_destructive": False})
        self.assertIsInstance(limits['max_voltage'], float)

    def test_null_body_clears(self):
        limits, error = nets_handler._validate_safety_limits(None)
        self.assertIsNone(error)
        self.assertEqual(limits, {})

    def test_explicit_null_drops_one_key(self):
        limits, error = nets_handler._validate_safety_limits(
            {"max_voltage": 5.0, "max_current": None})
        self.assertIsNone(error)
        self.assertEqual(limits, {"max_voltage": 5.0})

    def test_list_body_is_refused(self):
        limits, error = nets_handler._validate_safety_limits([1, 2])
        self.assertIsNone(limits)
        self.assertIn('JSON object', error)


class RouteTests(_NetsFileFixture):
    """The read-modify-write itself."""

    def test_sets_limits_and_leaves_every_other_field_alone(self):
        before = self.records_named('v33')[0]
        res = self.client.put('/nets/v33/safety-limits',
                              json={"max_voltage": 3.6, "max_current": 0.5})
        self.assertEqual(res.status_code, 200)

        after = self.records_named('v33')[0]
        self.assertEqual(after['safety_limits'],
                         {"max_voltage": 3.6, "max_current": 0.5})
        for key, value in before.items():
            if key == 'safety_limits':
                continue
            self.assertEqual(after[key], value, "field %r was disturbed" % key)

    def test_updates_every_record_sharing_the_name(self):
        res = self.client.put('/nets/vbat/safety-limits', json={"max_voltage": 4.2})
        self.assertEqual(res.status_code, 200)

        records = self.records_named('vbat')
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(record['safety_limits'], {"max_voltage": 4.2})

    def test_other_nets_are_untouched(self):
        self.client.put('/nets/vbat/safety-limits', json={"max_voltage": 4.2})
        self.assertNotIn('safety_limits', self.records_named('v33')[0])

    def test_empty_body_clears_the_limits(self):
        self.client.put('/nets/v33/safety-limits', json={"max_voltage": 3.6})
        self.assertIn('safety_limits', self.records_named('v33')[0])

        res = self.client.put('/nets/v33/safety-limits', json={})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('safety_limits', self.records_named('v33')[0])
        self.assertIsNone(res.get_json()['safety_limits'])

    def test_unknown_net_is_404(self):
        res = self.client.put('/nets/nosuchnet/safety-limits', json={"max_voltage": 5})
        self.assertEqual(res.status_code, 404)
        self.assertIn('nosuchnet', res.get_json()['error'])

    def test_refused_body_writes_nothing(self):
        before = self.read_file()
        res = self.client.put('/nets/v33/safety-limits',
                              json={"max_voltage": 3.6, "max_power": 10})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.read_file(), before)

    def test_response_reports_what_was_stored(self):
        res = self.client.put('/nets/v33/safety-limits', json={"max_voltage": 3})
        body = res.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['name'], 'v33')
        self.assertEqual(body['safety_limits'], {"max_voltage": 3.0})


class ContractTests(_NetsFileFixture):
    """What the route writes is what lager.safety enforces."""

    def test_written_ceiling_refuses_an_over_limit_setpoint(self):
        self.client.put('/nets/v33/safety-limits', json={"max_voltage": 3.6})

        with self.assertRaises(safety.SafetyViolation) as caught:
            safety.check('v33', 'voltage', args=[24.0])
        self.assertEqual(caught.exception.limit_value, 3.6)
        self.assertEqual(caught.exception.requested, 24.0)

    def test_written_ceiling_still_allows_a_legal_setpoint(self):
        self.client.put('/nets/v33/safety-limits', json={"max_voltage": 3.6})
        safety.check('v33', 'voltage', args=[3.3])

    def test_clearing_restores_an_unrestricted_net(self):
        self.client.put('/nets/v33/safety-limits', json={"max_voltage": 3.6})
        with self.assertRaises(safety.SafetyViolation):
            safety.check('v33', 'voltage', args=[24.0])

        self.client.put('/nets/v33/safety-limits', json={})
        safety.check('v33', 'voltage', args=[24.0])

    def test_allow_destructive_false_is_honoured_by_check_destructive(self):
        self.client.put('/nets/v33/safety-limits', json={"allow_destructive": False})

        with self.assertRaises(safety.DestructiveOperationRefused):
            safety.check_destructive({"name": "v33"}, "erase")

    def test_a_shared_name_is_enforced_whichever_record_the_index_keeps(self):
        # 'vbat' has a power-supply and a battery record. find_by_name keeps one
        # of them, and which one is not something a caller can predict, so this
        # asserts through `ovp` -- guarded for both roles and mapped to
        # max_voltage in each. Both records carrying the ceiling is what makes
        # the outcome the same either way.
        self.client.put('/nets/vbat/safety-limits', json={"max_voltage": 4.2})

        self.assertEqual(safety.limits_for_net('vbat'), {"max_voltage": 4.2})
        with self.assertRaises(safety.SafetyViolation):
            safety.check('vbat', 'ovp', args=[12.0])

    def test_a_shared_name_resolves_one_role_for_the_value_check(self):
        # Pins a sharp edge inherited from the interlock, not introduced here:
        # the guarded method set is chosen by the ONE role find_by_name resolves
        # to. `voltage` is a supply setter and not a battery one, so on a name
        # carrying both roles it is value-checked only if the index happens to
        # keep the supply record. Writing the ceiling to every record removes
        # the ambiguity about the limit; it cannot remove it about the role.
        self.client.put('/nets/vbat/safety-limits', json={"max_voltage": 4.2})

        role = safety.role_for_net('vbat')
        self.assertIn(role, ('power-supply', 'battery'))
        if role == 'power-supply':
            with self.assertRaises(safety.SafetyViolation):
                safety.check('vbat', 'voltage', args=[12.0])
        else:
            # Guarded, but rate-checked only -- there is no battery `voltage`
            # setter for a ceiling to apply to.
            safety.check('vbat', 'voltage', args=[12.0])


if __name__ == '__main__':
    unittest.main()
