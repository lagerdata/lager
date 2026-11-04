# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``GET|PUT /dut`` on the box HTTP server.

The route lets a control plane replace the ``dut_slots`` block of
``bench.json``, which until now only ``lager dut`` could write over SSH. Four
properties carry it:

**A write is a whole-list replacement that touches nothing else.** The other
keys of ``bench.json`` (overrides, constraints, interfaces) survive byte for
byte, and the single-DUT short form is retired so it cannot shadow the list.

**A slot the loader would skip is refused.** The bench loader drops a slot
with no name and logs a warning nobody sees; here that is a 400 naming the
slot, before anything is written.

**The clock is recorded.** ``dut_updated_at`` is the caller's value or now,
so the two sides can tell which copy is newer.

**A file that is not an object is never replaced.** Reading it is a 500, not
an empty bench to overwrite.
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
from lager.http_handlers import dut_handler  # noqa: E402


def _slot(name="main", **extra):
    return {
        "name": name, "purpose": "Power regression rig", "mcu": "STM32H7",
        "schematic_refs": [{"title": "Main board", "repo_path": "docs/sch.pdf",
                            "pages": "3", "revision": "B"}],
        "subsystems": [{"name": "Power tree", "nets": ["psu1"]}],
        **extra,
    }


class _DutRouteFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'bench.json')
        self._orig = dut_handler.BENCH_JSON_PATH
        dut_handler.BENCH_JSON_PATH = self.path
        app = Flask(__name__)
        dut_handler.register_dut_routes(app)
        self.client = app.test_client()

    def tearDown(self):
        dut_handler.BENCH_JSON_PATH = self._orig
        self.tmp.cleanup()

    def write_bench(self, payload):
        with open(self.path, 'w', encoding='utf-8') as f:
            if isinstance(payload, str):
                f.write(payload)
            else:
                json.dump(payload, f)

    def read_bench(self):
        with open(self.path, 'r', encoding='utf-8') as f:
            return json.load(f)


class ReadTests(_DutRouteFixture):

    def test_missing_file_reads_as_no_slots(self):
        res = self.client.get('/dut')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {'dut_slots': [], 'dut_updated_at': None})

    def test_reads_the_list_form_with_its_clock(self):
        self.write_bench({"dut_slots": [_slot()], "dut_updated_at": "2026-09-23T10:00:00Z"})
        got = self.client.get('/dut').get_json()
        self.assertEqual(got['dut_slots'][0]['name'], 'main')
        self.assertEqual(got['dut_updated_at'], '2026-09-23T10:00:00Z')

    def test_reads_the_short_form_as_a_list(self):
        self.write_bench({"dut_context": _slot("only")})
        got = self.client.get('/dut').get_json()
        self.assertEqual([s['name'] for s in got['dut_slots']], ['only'])

    def test_a_non_object_file_is_a_500_not_an_empty_bench(self):
        self.write_bench('["not", "an", "object"]')
        res = self.client.get('/dut')
        self.assertEqual(res.status_code, 500)
        self.assertIn('not a JSON object', res.get_json()['error'])


class WriteTests(_DutRouteFixture):

    def test_replaces_the_list_and_keeps_every_other_key(self):
        self.write_bench({
            "dut_context": _slot("old"),
            "net_overrides": [{"name": "psu1", "purpose": "override"}],
            "constraints": {"max_voltage": {"psu1": 5.0}},
            "interfaces": [{"name": "spi0", "protocol": "spi"}],
            "custom_key": 42,
        })
        res = self.client.put('/dut', json={
            "dut_slots": [_slot("main"), _slot("aux", active=False)],
            "updated_at": "2026-09-23T11:00:00Z",
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual([s['name'] for s in body['dut_slots']], ['main', 'aux'])
        self.assertEqual(body['dut_updated_at'], '2026-09-23T11:00:00Z')

        on_disk = self.read_bench()
        self.assertEqual([s['name'] for s in on_disk['dut_slots']], ['main', 'aux'])
        self.assertNotIn('dut_context', on_disk)
        self.assertEqual(on_disk['dut_updated_at'], '2026-09-23T11:00:00Z')
        self.assertEqual(on_disk['net_overrides'], [{"name": "psu1", "purpose": "override"}])
        self.assertEqual(on_disk['constraints'], {"max_voltage": {"psu1": 5.0}})
        self.assertEqual(on_disk['interfaces'], [{"name": "spi0", "protocol": "spi"}])
        self.assertEqual(on_disk['custom_key'], 42)
        # Unknown DocRef keys survive: the file is stored as given.
        self.assertEqual(on_disk['dut_slots'][0]['schematic_refs'][0]['revision'], 'B')

    def test_first_write_creates_the_file(self):
        res = self.client.put('/dut', json={"dut_slots": [_slot()]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.read_bench()['dut_slots'][0]['name'], 'main')

    def test_clock_defaults_to_now_in_utc(self):
        res = self.client.put('/dut', json={"dut_slots": []})
        stamp = res.get_json()['dut_updated_at']
        self.assertRegex(stamp, r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        self.assertEqual(self.read_bench()['dut_updated_at'], stamp)

    def test_empty_list_clears_the_dut_context(self):
        self.write_bench({"dut_slots": [_slot()]})
        self.assertEqual(self.client.put('/dut', json={"dut_slots": []}).status_code, 200)
        self.assertEqual(self.read_bench()['dut_slots'], [])

    def test_refused_payloads_write_nothing(self):
        self.write_bench({"dut_slots": [_slot("keep")]})
        cases = [
            ([1, 2], 'JSON object'),
            ({"dut_slots": "main"}, 'must be an array'),
            ({"dut_slots": ["main"]}, 'dut_slots[0] must be an object'),
            ({"dut_slots": [{"purpose": "no name"}]}, "dut_slots[0]: dut slot entry missing 'name'"),
            ({"dut_slots": [_slot("a"), _slot("a")]}, "duplicate DUT name 'a'"),
            ({"dut_slots": [_slot()], "updated_at": "  "}, 'updated_at must be'),
            ({"dut_slots": [_slot()], "updated_at": 7}, 'updated_at must be'),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload):
                res = self.client.put('/dut', json=payload)
                self.assertEqual(res.status_code, 400)
                self.assertIn(message, res.get_json()['error'])
        self.assertEqual([s['name'] for s in self.read_bench()['dut_slots']], ['keep'])

    def test_a_non_object_file_is_never_replaced(self):
        self.write_bench('["not", "an", "object"]')
        res = self.client.put('/dut', json={"dut_slots": [_slot()]})
        self.assertEqual(res.status_code, 500)
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), ["not", "an", "object"])


class ValidateSlotsTests(unittest.TestCase):

    def test_validation_matches_the_loader(self):
        # A slot the loader builds is accepted; the loader's own message names
        # the one it would refuse.
        slots, err = dut_handler.validate_slots({"dut_slots": [_slot()]})
        self.assertIsNone(err)
        self.assertEqual(slots[0]['name'], 'main')
        _, err = dut_handler.validate_slots({"dut_slots": [{}]})
        self.assertIn("missing 'name'", err)


if __name__ == '__main__':
    unittest.main()
