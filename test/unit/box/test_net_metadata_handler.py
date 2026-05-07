# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the PUT /nets/<name>/metadata handler.

Verifies merge semantics, per-field timestamp handling, and 404 behavior.
"""

import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted):
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'pigpio',
    'labjack', 'labjack.ljm',
    'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak',
    'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev',
    'smbus', 'smbus2',
    'RPi', 'RPi.GPIO',
    'gpiod',
    'flask_socketio',
]:
    _stub(_dep)

sys.modules['simplejson'] = sys.modules['json']  # type: ignore[assignment]

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from flask import Flask  # noqa: E402

from lager.http_handlers.net_metadata_handler import register_net_metadata_routes  # noqa: E402


def _build_app(saved_nets):
    """Build a Flask app whose registered routes see `saved_nets` as the on-disk
    state and write back into the same list.
    """
    state = {'nets': [dict(n) for n in saved_nets]}

    with patch('lager.http_handlers.net_metadata_handler.Net') as MockNet:
        MockNet.get_local_nets.side_effect = lambda: state['nets']

        def _save(new_nets):
            state['nets'] = [dict(n) for n in new_nets]

        MockNet.save_local_nets.side_effect = _save

        app = Flask(__name__)
        register_net_metadata_routes(app)
        client = app.test_client()
        yield client, state


def _put(client, name, body):
    return client.put(
        f'/nets/{name}/metadata',
        data=json.dumps(body),
        content_type='application/json',
    )


class TestNetMetadataHandler(unittest.TestCase):

    def test_merges_fields_and_timestamps(self):
        nets = [{'name': 'usb1', 'role': 'usb', 'instrument': 'Acroname'}]
        gen = _build_app(nets)
        client, state = next(gen)

        ts = '2026-05-06T12:00:00+00:00'
        resp = _put(client, 'usb1', {
            'fields': {'description': 'ACME power', 'tags': ['power', 'critical']},
            'timestamps': {'description': ts, 'tags': ts},
        })

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['metadata_timestamps']['description'], ts)
        self.assertEqual(body['metadata_timestamps']['tags'], ts)

        entry = state['nets'][0]
        self.assertEqual(entry['description'], 'ACME power')
        self.assertEqual(entry['tags'], ['power', 'critical'])
        self.assertEqual(entry['metadata_timestamps'], {'description': ts, 'tags': ts})

    def test_partial_update_preserves_other_field_timestamps(self):
        nets = [{
            'name': 'usb1', 'role': 'usb', 'instrument': 'Acroname',
            'description': 'old desc',
            'tags': ['power'],
            'metadata_timestamps': {
                'description': '2026-05-01T00:00:00+00:00',
                'tags': '2026-05-02T00:00:00+00:00',
            },
        }]
        gen = _build_app(nets)
        client, state = next(gen)

        new_ts = '2026-05-06T12:00:00+00:00'
        resp = _put(client, 'usb1', {
            'fields': {'description': 'new desc'},
            'timestamps': {'description': new_ts},
        })

        self.assertEqual(resp.status_code, 200)
        entry = state['nets'][0]
        self.assertEqual(entry['description'], 'new desc')
        self.assertEqual(entry['tags'], ['power'])  # untouched
        self.assertEqual(entry['metadata_timestamps']['description'], new_ts)
        self.assertEqual(entry['metadata_timestamps']['tags'], '2026-05-02T00:00:00+00:00')

    def test_404_when_net_missing(self):
        gen = _build_app([])
        client, _ = next(gen)

        resp = _put(client, 'doesnotexist', {
            'fields': {'description': 'x'},
            'timestamps': {'description': '2026-05-06T00:00:00+00:00'},
        })
        self.assertEqual(resp.status_code, 404)
        self.assertIn('not found', resp.get_json()['error'])

    def test_400_on_unknown_field(self):
        nets = [{'name': 'usb1', 'role': 'usb', 'instrument': 'Acroname'}]
        gen = _build_app(nets)
        client, _ = next(gen)

        resp = _put(client, 'usb1', {
            'fields': {'unknown_field': 'oops'},
            'timestamps': {},
        })
        self.assertEqual(resp.status_code, 400)

    def test_400_on_non_string_timestamp(self):
        nets = [{'name': 'usb1', 'role': 'usb', 'instrument': 'Acroname'}]
        gen = _build_app(nets)
        client, _ = next(gen)

        resp = _put(client, 'usb1', {
            'fields': {'description': 'x'},
            'timestamps': {'description': 12345},
        })
        self.assertEqual(resp.status_code, 400)

    def test_400_on_non_string_tags(self):
        nets = [{'name': 'usb1', 'role': 'usb', 'instrument': 'Acroname'}]
        gen = _build_app(nets)
        client, _ = next(gen)

        resp = _put(client, 'usb1', {
            'fields': {'tags': ['ok', 42]},
            'timestamps': {},
        })
        self.assertEqual(resp.status_code, 400)

    def test_null_description_clears_value(self):
        nets = [{
            'name': 'usb1', 'role': 'usb', 'instrument': 'Acroname',
            'description': 'old',
        }]
        gen = _build_app(nets)
        client, state = next(gen)

        resp = _put(client, 'usb1', {
            'fields': {'description': None},
            'timestamps': {'description': '2026-05-06T00:00:00+00:00'},
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(state['nets'][0]['description'])


if __name__ == '__main__':
    unittest.main()
