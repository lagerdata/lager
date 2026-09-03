# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for GET|PUT /box-metadata.

The box's own description lives in ``/etc/lager/box_metadata.json``, separate
from ``bench.json``. Three properties carry the endpoint:

**A box that has never been described reads as empty, not as an error.** The
``/status`` probe embeds this value, so a missing or truncated file must degrade
to "no description" rather than failing the health check the control plane uses
to decide the box is reachable at all.

**The write is atomic.** A crash mid-write must not leave a half-written file
where the next read expects JSON -- which is the same reason ``saved_nets.json``
stages through a temp file, and why this handler reuses that helper rather than
opening the destination directly.

**The timestamp round-trips.** Reconciliation is last-write-wins on
``updated_at``; a write that stored the description but dropped its timestamp
would make the box look permanently older than the control plane and get
overwritten on every probe.
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
from lager.http_handlers import box_metadata_handler  # noqa: E402


class _BoxMetadataFixture(unittest.TestCase):
    """Points the handler at a temp box_metadata.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'box_metadata.json')

        self._orig = box_metadata_handler.BOX_METADATA_PATH
        box_metadata_handler.BOX_METADATA_PATH = self.path

        app = Flask(__name__)
        box_metadata_handler.register_box_metadata_routes(app)
        self.client = app.test_client()

    def tearDown(self):
        box_metadata_handler.BOX_METADATA_PATH = self._orig
        self.tmp.cleanup()

    def write_raw(self, text):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(text)

    def read_file(self):
        with open(self.path, 'r', encoding='utf-8') as f:
            return json.load(f)


class ReadTests(_BoxMetadataFixture):

    def test_missing_file_reads_as_empty(self):
        res = self.client.get('/box-metadata')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {'description': None, 'updated_at': None})

    def test_truncated_file_reads_as_empty(self):
        # A bad shutdown must not make /status fail the reachability probe.
        self.write_raw('{"description": "half')
        self.assertEqual(
            self.client.get('/box-metadata').get_json(),
            {'description': None, 'updated_at': None},
        )

    def test_non_object_json_reads_as_empty(self):
        self.write_raw('["not", "an", "object"]')
        self.assertEqual(
            self.client.get('/box-metadata').get_json(),
            {'description': None, 'updated_at': None},
        )

    def test_wrong_typed_values_read_as_empty(self):
        self.write_raw('{"description": 42, "updated_at": []}')
        self.assertEqual(
            self.client.get('/box-metadata').get_json(),
            {'description': None, 'updated_at': None},
        )

    def test_unknown_keys_are_ignored(self):
        self.write_raw('{"description": "bench", "updated_at": "t", "extra": 1}')
        self.assertEqual(
            self.client.get('/box-metadata').get_json(),
            {'description': 'bench', 'updated_at': 't'},
        )


class WriteTests(_BoxMetadataFixture):

    def test_description_and_timestamp_round_trip(self):
        res = self.client.put('/box-metadata', json={
            'description': 'Hardware validation bench',
            'updated_at': '2026-09-03T10:00:00Z',
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['ok'])

        got = self.client.get('/box-metadata').get_json()
        self.assertEqual(got['description'], 'Hardware validation bench')
        self.assertEqual(got['updated_at'], '2026-09-03T10:00:00Z')

    def test_write_creates_the_file_and_its_directory(self):
        nested = os.path.join(self.tmp.name, 'etc', 'lager', 'box_metadata.json')
        box_metadata_handler.BOX_METADATA_PATH = nested
        self.client.put('/box-metadata', json={'description': 'x', 'updated_at': 't'})
        self.assertTrue(os.path.exists(nested))

    def test_null_description_clears_it(self):
        self.client.put('/box-metadata', json={'description': 'x', 'updated_at': 't'})
        self.client.put('/box-metadata', json={'description': None, 'updated_at': 't2'})
        self.assertIsNone(self.client.get('/box-metadata').get_json()['description'])

    def test_no_temp_file_is_left_behind(self):
        self.client.put('/box-metadata', json={'description': 'x', 'updated_at': 't'})
        self.assertFalse(os.path.exists(self.path + '.tmp'))

    def test_non_object_body_is_refused(self):
        res = self.client.put('/box-metadata', json=['nope'])
        self.assertEqual(res.status_code, 400)
        self.assertIn('JSON object', res.get_json()['error'])

    def test_non_string_description_is_refused(self):
        res = self.client.put('/box-metadata', json={'description': 42})
        self.assertEqual(res.status_code, 400)
        self.assertIn('description must be a string or null', res.get_json()['error'])

    def test_non_string_timestamp_is_refused(self):
        res = self.client.put('/box-metadata', json={'description': 'x', 'updated_at': 5})
        self.assertEqual(res.status_code, 400)
        self.assertIn('ISO 8601', res.get_json()['error'])

    def test_refused_body_leaves_the_stored_value_alone(self):
        self.client.put('/box-metadata', json={'description': 'keep me', 'updated_at': 't'})
        self.client.put('/box-metadata', json={'description': 42})
        self.assertEqual(self.read_file()['description'], 'keep me')


if __name__ == '__main__':
    unittest.main()
