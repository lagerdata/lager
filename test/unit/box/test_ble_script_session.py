# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the on-box script API to BLE sessions
(box/lager/protocols/ble/session.py: ``from lager.ble import Session``).

The session talks to the box's own /ble namespace over localhost; here a fake
socketio.Client plays the box, answering each request as ble_session.py does.
``adapter()``/``scan()`` go to /ble/command with ``requests.post`` patched.
Also covers Central.connect handing bleak BlueZ's record of a known device.
"""

import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


for _dep in ('bleak', 'flask_socketio'):
    if _dep not in sys.modules:
        sys.modules[_dep] = _make_module(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager import ble as lager_ble  # noqa: E402
from lager.protocols.ble import client as ble_client  # noqa: E402
from lager.protocols.ble import session as ble_session  # noqa: E402

ADDR = 'AA:BB:CC:DD:EE:01'
NOTIFY = '12345678-1234-5678-1234-56789abcdef1'
WRITE = '12345678-1234-5678-1234-56789abcdef2'
OPENED = {'address': ADDR, 'mtu': 517, 'mtu_source': 'bluez', 'services': [
    {'uuid': 'svc', 'description': 's', 'characteristics': [
        {'uuid': NOTIFY, 'handle': 10, 'description': 'n', 'properties': ['notify']}]}]}


class FakeBox:
    """A socketio.Client stand-in that plays the box's /ble namespace."""

    def __init__(self, replies=None):
        self.handlers = {}
        self.sent = []
        self.replies = replies or {}
        self.e = 0
        self.disconnected = False

    def on(self, event, handler=None, namespace=None):
        self.handlers[event] = handler

    def connect(self, url, namespaces=None, wait_timeout=None, headers=None):
        self.url = url

    def disconnect(self):
        self.disconnected = True

    def deliver(self, event, payload):
        self.e += 1
        self.handlers[event](dict(payload, e=self.e))

    def emit(self, event, data=None, namespace=None):
        assert namespace == '/ble'
        self.sent.append((event, dict(data)))
        op = event[len('ble_'):]
        for name, payload in self.replies.get(op, self.default)(op, data):
            if name == 'ble_result':
                payload = dict(payload, seq=data['seq'])
            self.deliver(name, payload)

    @staticmethod
    def ok(value=None):
        return [('ble_result', {'ok': True, 'value': value or {}})]

    def default(self, op, data):
        if op == 'open':
            return self.ok(OPENED)
        if op == 'read':
            return self.ok({'data': 'cafe'})
        if op == 'close':
            return self.ok() + [('ble_closed', {'reason': 'client', 'message': ''})]
        return self.ok({'op': op})


def notify(*items):
    return ('ble_notify', {'items': [
        {'n': n, 'char': NOTIFY, 'handle': 10, 'ts': 1700000000.0, 'data': d}
        for n, d in items]})


def open_session(replies=None, **kwargs):
    fake = FakeBox(replies)
    s = lager_ble.Session(ADDR, sio=fake, op_timeout=1.0, **kwargs)
    return s, fake


class TestSession(unittest.TestCase):

    def test_open_reports_mtu_and_names_the_script_as_holder(self):
        with patch.dict(os.environ, {'LAGER_PROCESS_ID': 'proc-1'}):
            s, fake = open_session()
        self.assertEqual(fake.url, 'http://127.0.0.1:9000')
        self.assertEqual((s.mtu, s.mtu_is_measured, s.max_write_len), (517, True, 512))
        self.assertEqual(s.address, ADDR)
        self.assertEqual(fake.sent[0], ('ble_open', {
            'seq': 1, 'address': ADDR, 'connect_timeout': 10.0, 'idle_timeout': 300.0,
            'holder': 'lager python proc-1'}))

    def test_open_failure_raises_with_the_code_and_disconnects(self):
        fake = FakeBox({'open': lambda op, d: [('ble_result', {
            'ok': False, 'code': 'adapter_busy', 'message': 'in use'})]})
        with self.assertRaises(lager_ble.SessionError) as info:
            lager_ble.Session(ADDR, sio=fake, op_timeout=1.0)
        self.assertEqual(info.exception.code, 'adapter_busy')
        self.assertTrue(fake.disconnected)

    def test_operations_and_fields(self):
        s, fake = open_session()
        s.subscribe(NOTIFY)
        s.write(WRITE, b'\x01\x02', response=False)
        s.write(None, b'\x00' * 600, chunk_size=100, handle=12)
        self.assertEqual(s.read(handle=14), b'\xca\xfe')
        s.ping()
        self.assertEqual([e for e, _ in fake.sent],
                         ['ble_open', 'ble_subscribe', 'ble_write', 'ble_write',
                          'ble_read', 'ble_ping'])
        self.assertEqual(fake.sent[2][1], {'seq': 3, 'char': WRITE, 'data': '0102',
                                           'response': False, 'chunk': False})
        chunked = fake.sent[3][1]
        self.assertEqual((chunked['handle'], chunked['chunk'], chunked['chunk_size']),
                         (12, True, 100))

    def test_notifications_in_box_order_even_if_handled_out_of_order(self):
        s, fake = open_session()
        handler = fake.handlers['ble_notify']
        # e=2 handled before e=1: python-socketio runs each on its own thread.
        handler({'e': fake.e + 2, 'items': [
            {'n': 2, 'char': NOTIFY, 'handle': 10, 'ts': 0.0, 'data': '02'}]})
        self.assertIsNone(s.try_recv())
        handler({'e': fake.e + 1, 'items': [
            {'n': 1, 'char': NOTIFY, 'handle': 10, 'ts': 0.0, 'data': '01'}]})
        self.assertEqual([s.recv(1).data, s.recv(1).data], [b'\x01', b'\x02'])

    def test_recv_timeout_and_drain_before_closed(self):
        s, fake = open_session({'subscribe': lambda op, d: FakeBox.ok() + [
            notify((1, 'aa'), (2, 'bb')),
            ('ble_closed', {'reason': 'disconnected', 'message': 'gone'})]})
        s.subscribe(NOTIFY)
        first = s.recv(1)
        self.assertEqual((first.seq, first.char, first.handle, first.data),
                         (1, NOTIFY, 10, b'\xaa'))
        self.assertEqual(s.recv(1).data, b'\xbb')
        with self.assertRaises(lager_ble.SessionClosed) as info:
            s.recv(1)
        self.assertEqual(info.exception.code, 'disconnected')
        self.assertEqual(s.close_reason, 'disconnected')

    def test_recv_times_out(self):
        s, _ = open_session()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            s.recv(0.3)
        self.assertLess(time.monotonic() - start, 2)

    def test_context_manager_closes(self):
        fake = FakeBox()
        with lager_ble.Session(ADDR, sio=fake, op_timeout=1.0) as s:
            pass
        self.assertEqual(fake.sent[-1][0], 'ble_close')
        self.assertEqual(s.close_reason, 'client')
        self.assertTrue(fake.disconnected)

    def test_lost_connection_fails_the_waiting_call(self):
        fake = FakeBox({'read': lambda op, d: []})
        s = lager_ble.Session(ADDR, sio=fake, op_timeout=5.0)
        threading.Timer(0.2, fake.handlers['disconnect']).start()
        with self.assertRaises(lager_ble.SessionClosed) as info:
            s.read(NOTIFY)
        self.assertEqual(info.exception.code, 'connection_lost')


class TestCommands(unittest.TestCase):

    def _post(self, body):
        resp = MagicMock(status_code=200)
        resp.json.return_value = body
        return patch('requests.post', return_value=resp)

    def test_adapter(self):
        value = {'available': True, 'adapters': [], 'reason': None}
        with self._post({'success': True, 'value': value}) as post:
            self.assertEqual(lager_ble.adapter(), value)
        self.assertEqual(post.call_args[0][0], 'http://127.0.0.1:9000/ble/command')
        self.assertEqual(post.call_args[1]['json'], {'action': 'adapter', 'params': {}})

    def test_scan_returns_devices_with_address_types(self):
        devices = [{'name': 'p', 'address': ADDR, 'address_type': 'random',
                    'random_type': 'static', 'rssi': -40, 'uuids': []}]
        with self._post({'success': True, 'value': {'devices': devices}}) as post:
            self.assertEqual(lager_ble.scan(3.0, name_contains='p'), devices)
        self.assertEqual(post.call_args[1]['json']['params'],
                         {'timeout': 3.0, 'name_contains': 'p'})

    def test_scan_during_a_session_raises_adapter_busy(self):
        body = {'success': False, 'code': 'adapter_busy', 'error': 'in use'}
        with self._post(body):
            with self.assertRaises(lager_ble.SessionError) as info:
                lager_ble.scan()
        self.assertEqual(info.exception.code, 'adapter_busy')


class TestCentralUsesKnownDevice(unittest.TestCase):

    def test_connect_hands_bleak_the_bluez_record(self):
        record = object()

        async def fake_target(address):
            return record

        loop = __import__('asyncio').new_event_loop()
        self.addCleanup(loop.close)
        with patch('lager.protocols.ble.target.ble_target', fake_target), \
                patch.object(ble_client, 'BleakClient') as bleak_client, \
                patch.object(ble_client.Client, 'connect', return_value=True):
            ble_client.Central(loop=loop).connect(ADDR)
        bleak_client.assert_called_once_with(record)


if __name__ == '__main__':
    unittest.main()
