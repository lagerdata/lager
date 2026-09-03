# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for GET|PUT /nets/<name>/metadata.

The endpoint exists so the control plane can sync a net's ``purpose`` / ``notes``
/ ``tags`` without modelling the whole net record. Four properties carry it, and
each is pinned here:

**A metadata write disturbs nothing else.** ``PUT /nets/<name>`` replaces a net
wholesale; this route merges. A caller that only knows about prose must not be
able to drop ``jlink_script``, ``safety_limits`` or ``usb_identity`` -- which is
exactly how the Net-Manager TUI used to lose them.

**Every record sharing the name is updated.** The MCP bench loader builds one
descriptor per record, so leaving a same-named sibling behind would make which
metadata an agent sees depend on file order.

**A refused body changes nothing.** Validation runs before the
read-modify-write, so a partially-valid payload cannot leave half its fields
applied -- and, because ``get_local_nets`` hands back the cache's own dicts, a
refused or failed write must not leave the new values live in memory either.

**A field overridden in bench.json is reported, not silently swallowed.** The
bench loader applies ``net_overrides`` after reading ``saved_nets.json``, so a
write under one lands on disk and never reaches an agent. Answering a bare
``{"ok": true}`` there would tell the control plane a value synced when it did
not.
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
from lager.http_handlers import net_metadata_handler  # noqa: E402
from lager.nets import net as net_module  # noqa: E402


def _saved_nets():
    """'uart1' carries side-car fields a metadata write must not disturb.

    'vbat' deliberately appears twice, as a power-supply and a battery -- the
    same shape the safety-limits endpoint guards against.
    """
    return [
        {
            "name": "uart1", "role": "uart", "instrument": "SiLabs_CP210x",
            "address": "USB0::0x10C4::0xEA60::SER1::INSTR", "pin": "/dev/ttyUSB0",
            "usb_identity": {"vid": "10c4", "pid": "ea60", "serial": "SER1"},
            "jlink_script": "Zm9v",
            "safety_limits": {"max_voltage": 3.6},
            "params": {"baud": 115200},
        },
        {"name": "vbat", "role": "power-supply", "instrument": "Keysight_E36313A",
         "address": "USB0::0x2A8D::0x1002::MY1::INSTR", "channel": 1, "pin": 0},
        {"name": "vbat", "role": "battery", "instrument": "Keysight_E36731A",
         "address": "USB0::0x2A8D::0x1102::MY2::INSTR", "channel": 1, "pin": 0},
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

        # No bench.json unless a test writes one.
        self.bench_path = os.path.join(self.tmp.name, 'bench.json')
        self._orig_bench = net_metadata_handler._BENCH_JSON_PATH
        net_metadata_handler._BENCH_JSON_PATH = self.bench_path

        app = Flask(__name__)
        net_metadata_handler.register_net_metadata_routes(app)
        self.client = app.test_client()

    def tearDown(self):
        net_module.LOCAL_NETS_PATH = self._orig_write_path
        net_metadata_handler._BENCH_JSON_PATH = self._orig_bench
        self.cache._path = self._orig_cache_path
        self.cache.invalidate()
        self.tmp.cleanup()

    def read_file(self):
        with open(self.path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def record(self, name):
        return [n for n in self.read_file() if n.get('name') == name][0]

    def records_named(self, name):
        return [n for n in self.read_file() if n.get('name') == name]

    def write_bench(self, payload):
        with open(self.bench_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f)


class ValidationTests(unittest.TestCase):
    """_validate_payload, exercised directly -- no filesystem."""

    def test_non_object_body_is_refused(self):
        self.assertIn('JSON object', net_metadata_handler._validate_payload([1, 2]))

    def test_unknown_field_is_refused_and_names_the_alternatives(self):
        err = net_metadata_handler._validate_payload({"fields": {"description": "x"}})
        self.assertIn('description', err)
        self.assertIn('purpose', err)

    def test_pre_v0_24_vocabulary_is_refused(self):
        # `description`/`dut_connection`/`test_hints` were renamed in v0.24.0.
        # Accepting them would write keys nothing on the box reads back.
        for legacy in ('description', 'dut_connection', 'test_hints'):
            with self.subTest(field=legacy):
                err = net_metadata_handler._validate_payload({"fields": {legacy: "x"}})
                self.assertIsNotNone(err)

    def test_purpose_must_be_a_string(self):
        err = net_metadata_handler._validate_payload({"fields": {"purpose": 42}})
        self.assertIn('purpose must be a string or null', err)

    def test_tags_must_be_strings(self):
        err = net_metadata_handler._validate_payload({"fields": {"tags": ["ok", 3]}})
        self.assertIn('tags must be an array of strings', err)

    def test_timestamp_must_be_a_non_empty_string(self):
        err = net_metadata_handler._validate_payload(
            {"fields": {"purpose": "x"}, "timestamps": {"purpose": "  "}}
        )
        self.assertIn('ISO 8601', err)

    def test_timestamp_without_its_field_is_refused(self):
        # Advancing the clock on a field this request did not write would make
        # the next probe treat a stale value as the newer one.
        err = net_metadata_handler._validate_payload(
            {"fields": {"purpose": "x"}, "timestamps": {"tags": "2026-09-03T00:00:00Z"}}
        )
        self.assertIn('without a matching field', err)

    def test_empty_payload_is_accepted(self):
        self.assertIsNone(net_metadata_handler._validate_payload({}))


class WriteTests(_NetsFileFixture):

    def test_metadata_round_trips(self):
        res = self.client.put('/nets/uart1/metadata', json={
            "fields": {"purpose": "DUT console", "notes": "3v3 only", "tags": ["a", "b"]},
            "timestamps": {"purpose": "2026-09-03T10:00:00Z",
                           "notes": "2026-09-03T10:00:00Z",
                           "tags": "2026-09-03T10:00:00Z"},
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['ok'])

        rec = self.record('uart1')
        self.assertEqual(rec['purpose'], 'DUT console')
        self.assertEqual(rec['notes'], '3v3 only')
        self.assertEqual(rec['tags'], ['a', 'b'])
        self.assertEqual(rec['metadata_timestamps']['purpose'], '2026-09-03T10:00:00Z')

        got = self.client.get('/nets/uart1/metadata').get_json()
        self.assertEqual(got['fields']['purpose'], 'DUT console')
        self.assertEqual(got['fields']['tags'], ['a', 'b'])

    def test_write_preserves_every_other_field(self):
        # The regression that motivates the endpoint: a metadata edit must not
        # be able to drop a debug script or a safety ceiling.
        before = self.record('uart1')
        self.client.put('/nets/uart1/metadata', json={"fields": {"purpose": "p"}})
        after = self.record('uart1')

        for key in ('role', 'instrument', 'address', 'pin', 'usb_identity',
                    'jlink_script', 'safety_limits', 'params'):
            self.assertEqual(after[key], before[key], f'{key} was disturbed')

    def test_partial_write_leaves_other_metadata_alone(self):
        self.client.put('/nets/uart1/metadata', json={
            "fields": {"purpose": "p", "tags": ["x"]}})
        self.client.put('/nets/uart1/metadata', json={"fields": {"notes": "n"}})

        rec = self.record('uart1')
        self.assertEqual(rec['purpose'], 'p')
        self.assertEqual(rec['tags'], ['x'])
        self.assertEqual(rec['notes'], 'n')

    def test_null_clears_a_field(self):
        self.client.put('/nets/uart1/metadata', json={"fields": {"purpose": "p"}})
        self.client.put('/nets/uart1/metadata', json={"fields": {"purpose": None}})
        self.assertNotIn('purpose', self.record('uart1'))

    def test_empty_tag_list_is_stored_not_dropped(self):
        # `lager nets describe --clear-tags` writes [], and "cleared" has to be
        # distinguishable from "never set" for reconciliation to converge.
        self.client.put('/nets/uart1/metadata', json={"fields": {"tags": ["x"]}})
        self.client.put('/nets/uart1/metadata', json={"fields": {"tags": []}})
        self.assertEqual(self.record('uart1')['tags'], [])

    def test_timestamps_merge_rather_than_replace(self):
        self.client.put('/nets/uart1/metadata', json={
            "fields": {"purpose": "p"}, "timestamps": {"purpose": "2026-09-01T00:00:00Z"}})
        self.client.put('/nets/uart1/metadata', json={
            "fields": {"notes": "n"}, "timestamps": {"notes": "2026-09-02T00:00:00Z"}})

        stamps = self.record('uart1')['metadata_timestamps']
        self.assertEqual(stamps['purpose'], '2026-09-01T00:00:00Z')
        self.assertEqual(stamps['notes'], '2026-09-02T00:00:00Z')

    def test_every_record_sharing_the_name_is_updated(self):
        res = self.client.put('/nets/vbat/metadata', json={"fields": {"purpose": "battery rail"}})
        self.assertEqual(res.get_json()['records_updated'], 2)

        records = self.records_named('vbat')
        self.assertEqual(len(records), 2)
        for rec in records:
            self.assertEqual(rec['purpose'], 'battery rail')

    def test_unknown_net_is_404(self):
        res = self.client.put('/nets/nope/metadata', json={"fields": {"purpose": "p"}})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.client.get('/nets/nope/metadata').status_code, 404)

    def test_get_reports_empty_defaults_for_an_undescribed_net(self):
        got = self.client.get('/nets/uart1/metadata').get_json()
        self.assertEqual(got['fields'], {'purpose': '', 'notes': '', 'tags': []})
        self.assertEqual(got['metadata_timestamps'], {})


class RefusedWriteTests(_NetsFileFixture):

    def test_refused_body_touches_neither_disk_nor_cache(self):
        res = self.client.put('/nets/uart1/metadata', json={"fields": {"purpose": 42}})
        self.assertEqual(res.status_code, 400)
        self.assertNotIn('purpose', self.record('uart1'))
        # get_local_nets returns the cache's own dicts; a refused write must not
        # have reached them either.
        cached = [n for n in net_module.Net.get_local_nets() if n.get('name') == 'uart1'][0]
        self.assertNotIn('purpose', cached)

    def test_failed_write_does_not_leave_values_live_in_the_cache(self):
        with patch.object(net_module.Net, 'save_local_nets', side_effect=OSError('disk full')):
            res = self.client.put('/nets/uart1/metadata', json={"fields": {"purpose": "p"}})
        self.assertEqual(res.status_code, 500)

        # Deliberately NOT invalidated: the point is that the handler mutated
        # copies, so the cache's own dicts never saw the failed write.
        cached = [n for n in net_module.Net.get_local_nets() if n.get('name') == 'uart1'][0]
        self.assertNotIn('purpose', cached)


class BenchOverrideTests(_NetsFileFixture):

    def test_overridden_field_is_reported_as_shadowed(self):
        self.write_bench({"net_overrides": [{"name": "uart1", "purpose": "from bench.json"}]})
        res = self.client.put('/nets/uart1/metadata', json={
            "fields": {"purpose": "from the dashboard", "notes": "n"}})

        body = res.get_json()
        self.assertEqual(body['shadowed_by_override'], ['purpose'])
        # The write still lands -- bench.json may be edited away later.
        self.assertEqual(self.record('uart1')['purpose'], 'from the dashboard')

    def test_unshadowed_fields_are_not_reported(self):
        self.write_bench({"net_overrides": [{"name": "uart1", "purpose": "x"}]})
        res = self.client.put('/nets/uart1/metadata', json={"fields": {"notes": "n"}})
        self.assertEqual(res.get_json()['shadowed_by_override'], [])

    def test_override_on_another_net_is_ignored(self):
        self.write_bench({"net_overrides": [{"name": "vbat", "purpose": "x"}]})
        res = self.client.put('/nets/uart1/metadata', json={"fields": {"purpose": "p"}})
        self.assertEqual(res.get_json()['shadowed_by_override'], [])

    def test_malformed_bench_json_is_not_fatal(self):
        with open(self.bench_path, 'w', encoding='utf-8') as f:
            f.write('{not json')
        res = self.client.put('/nets/uart1/metadata', json={"fields": {"purpose": "p"}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['shadowed_by_override'], [])


if __name__ == '__main__':
    unittest.main()
