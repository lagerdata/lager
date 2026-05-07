# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the GET/PUT /box-metadata handler."""

import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock


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


for _dep in ['pyvisa', 'usb', 'usb.util', 'usb.core', 'pigpio',
             'labjack', 'labjack.ljm', 'serial', 'serial.tools', 'serial.tools.list_ports',
             'flask_socketio']:
    _stub(_dep)

sys.modules['simplejson'] = sys.modules['json']  # type: ignore[assignment]

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

import tempfile  # noqa: E402

from flask import Flask  # noqa: E402

from lager.http_handlers import box_metadata_handler as bmh  # noqa: E402


def _build_client(tmp_path):
    bmh.BOX_METADATA_PATH = tmp_path
    app = Flask(__name__)
    bmh.register_box_metadata_routes(app)
    return app.test_client()


class TestBoxMetadataHandler(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
        self._tmp.close()
        os.unlink(self._tmp.name)  # start with no file
        self._original_path = bmh.BOX_METADATA_PATH

    def tearDown(self):
        bmh.BOX_METADATA_PATH = self._original_path
        if os.path.exists(self._tmp.name):
            os.unlink(self._tmp.name)

    def test_get_returns_null_defaults_when_file_missing(self):
        client = _build_client(self._tmp.name)
        resp = client.get('/box-metadata')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'description': None, 'updated_at': None})

    def test_put_then_get_round_trip(self):
        client = _build_client(self._tmp.name)
        ts = '2026-05-06T12:00:00+00:00'

        put_resp = client.put('/box-metadata', data=json.dumps({
            'description': 'PRD-2 J-Link board',
            'updated_at': ts,
        }), content_type='application/json')
        self.assertEqual(put_resp.status_code, 200)
        self.assertTrue(put_resp.get_json()['ok'])

        get_resp = client.get('/box-metadata')
        self.assertEqual(get_resp.status_code, 200)
        body = get_resp.get_json()
        self.assertEqual(body['description'], 'PRD-2 J-Link board')
        self.assertEqual(body['updated_at'], ts)

    def test_put_overwrites_previous_value(self):
        client = _build_client(self._tmp.name)
        client.put('/box-metadata', data=json.dumps({
            'description': 'first', 'updated_at': '2026-05-01T00:00:00+00:00',
        }), content_type='application/json')
        client.put('/box-metadata', data=json.dumps({
            'description': 'second', 'updated_at': '2026-05-02T00:00:00+00:00',
        }), content_type='application/json')
        body = client.get('/box-metadata').get_json()
        self.assertEqual(body['description'], 'second')
        self.assertEqual(body['updated_at'], '2026-05-02T00:00:00+00:00')

    def test_put_accepts_null_description(self):
        client = _build_client(self._tmp.name)
        resp = client.put('/box-metadata', data=json.dumps({
            'description': None, 'updated_at': '2026-05-06T00:00:00+00:00',
        }), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(client.get('/box-metadata').get_json()['description'])

    def test_put_rejects_non_string_description(self):
        client = _build_client(self._tmp.name)
        resp = client.put('/box-metadata', data=json.dumps({
            'description': 42, 'updated_at': '2026-05-06T00:00:00+00:00',
        }), content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_get_handles_invalid_json_in_file(self):
        with open(self._tmp.name, 'w') as f:
            f.write('not valid json {{')
        client = _build_client(self._tmp.name)
        resp = client.get('/box-metadata')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'description': None, 'updated_at': None})


if __name__ == '__main__':
    unittest.main()
