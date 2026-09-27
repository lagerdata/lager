# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``GET /bench`` on the box HTTP server.

The route serves the bench manifest -- the whole bench as one versioned
document -- to clients that keep a copy per box. Three properties carry it:

**The body is the manifest the MCP server would describe.** Same loaded
state, same schema, same ``box_id``.

**An unchanged manifest costs nothing.** ``ETag`` is the content hash and a
matching ``If-None-Match`` gets ``304`` with no body, in every spelling a
client might send (quoted, weak, bare, a list, ``*``).

**A broken bench never takes the route down silently.** A build failure is a
``500`` with the reason, not a 200 with an empty bench.
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
from lager.http_handlers import bench_manifest_handler  # noqa: E402
from lager.mcp import server_state  # noqa: E402
from lager.mcp.engine.bench_loader import load_from_dicts  # noqa: E402


class _BenchRouteFixture(unittest.TestCase):
    """Injects a bench into the MCP state module and registers the route."""

    def setUp(self):
        self._saved_state = server_state._state
        server_state.init_state(bench=load_from_dicts(
            raw_nets=[
                {"name": "psu1", "role": "power-supply", "instrument": "Rigol_DP832",
                 "purpose": "DUT 3V3 rail", "dut_connection": "J1 pin 2"},
                {"name": "dbg", "role": "debug", "instrument": "J-Link"},
            ],
            hello_data={"box_id": "BX-9", "hostname": "bx9", "version": "0.50.0"},
        ))
        app = Flask(__name__)
        bench_manifest_handler.register_bench_manifest_routes(app)
        self.client = app.test_client()

    def tearDown(self):
        server_state._state = self._saved_state


class BodyTests(_BenchRouteFixture):

    def test_serves_the_manifest_for_the_loaded_bench(self):
        res = self.client.get('/bench')
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body['schema_version'], 1)
        self.assertEqual(body['box_id'], 'BX-9')
        self.assertEqual(body['bench']['box_id'], 'BX-9')
        self.assertEqual([n['name'] for n in body['bench']['nets']], ['psu1', 'dbg'])
        psu = body['bench']['nets'][0]
        self.assertEqual(psu['dut_connection'], 'J1 pin 2')
        self.assertEqual(body['reference_keys'], {'psu1': 'PowerSupply', 'dbg': 'Debug'})
        self.assertEqual(body['bench']['metadata_sources']['psu1'],
                         {'purpose': 'saved_net', 'dut_connection': 'saved_net'})
        self.assertTrue(any(b['target'] == 'dbg' and b['role'] == 'flash_firmware'
                            for b in body['bench']['capability_bindings']))

    def test_etag_is_the_quoted_content_hash_and_caching_is_revalidate_only(self):
        res = self.client.get('/bench')
        body = res.get_json()
        self.assertEqual(res.headers['ETag'], '"%s"' % body['content_hash'])
        self.assertEqual(res.headers['Cache-Control'], 'no-cache')
        self.assertEqual(len(body['content_hash']), 64)

    def test_two_reads_of_an_unchanged_bench_hash_the_same(self):
        a = self.client.get('/bench').get_json()
        b = self.client.get('/bench').get_json()
        self.assertEqual(a['content_hash'], b['content_hash'])


class ConditionalTests(_BenchRouteFixture):

    def _etag(self):
        return self.client.get('/bench').headers['ETag']

    def test_matching_if_none_match_is_304_with_no_body(self):
        etag = self._etag()
        res = self.client.get('/bench', headers={'If-None-Match': etag})
        self.assertEqual(res.status_code, 304)
        self.assertEqual(res.data, b'')
        self.assertEqual(res.headers['ETag'], etag)

    def test_weak_bare_and_listed_forms_match_too(self):
        etag = self._etag()
        bare = etag.strip('"')
        for header in ('W/' + etag, bare, '"other", ' + etag, '*'):
            with self.subTest(header=header):
                res = self.client.get('/bench', headers={'If-None-Match': header})
                self.assertEqual(res.status_code, 304)

    def test_a_stale_etag_gets_the_body(self):
        res = self.client.get('/bench', headers={'If-None-Match': '"deadbeef"'})
        self.assertEqual(res.status_code, 200)
        self.assertIn('bench', res.get_json())

    def test_etag_matches_unit(self):
        m = bench_manifest_handler.etag_matches
        self.assertFalse(m(None, 'abc'))
        self.assertFalse(m('', 'abc'))
        self.assertTrue(m('"abc"', 'abc'))
        self.assertTrue(m('W/"abc"', 'abc'))
        self.assertTrue(m('abc', 'abc'))
        self.assertTrue(m('"x", "abc"', 'abc'))
        self.assertTrue(m('*', 'abc'))
        self.assertFalse(m('"abcd"', 'abc'))


class FailureTests(_BenchRouteFixture):

    def test_a_build_failure_is_a_500_that_says_why(self):
        orig = bench_manifest_handler._build_manifest

        def boom():
            raise ValueError("bench.json is not an object")

        bench_manifest_handler._build_manifest = boom
        try:
            res = self.client.get('/bench')
        finally:
            bench_manifest_handler._build_manifest = orig
        self.assertEqual(res.status_code, 500)
        self.assertIn('bench.json is not an object', res.get_json()['error'])


class LazyLoadTests(unittest.TestCase):
    """The :9000 server never calls init_state; the first request must."""

    def setUp(self):
        self._saved_state = server_state._state
        server_state._state = None

    def tearDown(self):
        server_state._state = self._saved_state

    def test_first_request_loads_state_from_disk(self):
        from lager.mcp.engine import bench_loader, instruments
        from lager.mcp.schemas.bench import BenchDefinition

        calls = {"n": 0}

        def fake_load_from_files():
            calls["n"] += 1
            return BenchDefinition(box_id="lazy-box")

        orig_load = bench_loader.load_from_files
        orig_scan = instruments.cached_instruments
        bench_loader.load_from_files = fake_load_from_files
        instruments.cached_instruments = lambda: []
        try:
            app = Flask(__name__)
            bench_manifest_handler.register_bench_manifest_routes(app)
            client = app.test_client()
            first = client.get('/bench').get_json()
            second = client.get('/bench').get_json()
        finally:
            bench_loader.load_from_files = orig_load
            instruments.cached_instruments = orig_scan
        self.assertEqual(first['box_id'], 'lazy-box')
        self.assertEqual(second['box_id'], 'lazy-box')
        self.assertEqual(calls["n"], 1)
