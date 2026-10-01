# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for BLE GATT sessions (box/lager/http_handlers/ble_session.py).

The channel runs on the real shared bleak event loop, but bleak itself is
replaced: ``_make_client`` returns a scripted FakeClient, and events go to a
recording emit function instead of Socket.IO. flask_socketio is not needed:
the module only uses it through the SocketIO instance it is handed.

Covered:
  - open: MTU from the BlueZ property / default fallback, services with
    handles, device-not-found / connect failure / BlueZ-missing codes
  - ordering: out-of-order seq is reordered; an unfilled gap fails with
    protocol_error; reused seq is refused
  - subscribe before write, notification during CCCD write is kept,
    notifications carry n/char/handle/ts/data
  - writes: property checks, chunking to mtu-3 in order, response flag
  - unknown / ambiguous characteristics, handle addressing, short UUIDs
  - link loss: queued notifications first, in-flight op -> disconnected,
    then ble_closed
  - idle timeout (notifications do not reset it, ping does)
  - overflow ends the session
  - adapter ownership: /ble/command and /blufi/command get 409, a second
    session gets adapter_busy, the lock is released on close
  - /ble/sessions list + release
"""

import asyncio
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

from flask import Flask  # noqa: E402
from lager.http_handlers import ble as ble_handler  # noqa: E402
from lager.http_handlers import blufi as blufi_handler  # noqa: E402
from lager.http_handlers import ble_session  # noqa: E402

ADDR = 'AA:BB:CC:DD:EE:01'
SVC = '12345678-1234-5678-1234-56789abcdef0'
NOTIFY = '12345678-1234-5678-1234-56789abcdef1'
WRITE = '12345678-1234-5678-1234-56789abcdef2'
READ = '12345678-1234-5678-1234-56789abcdef3'
DUP = '12345678-1234-5678-1234-56789abcdef4'
BATTERY_LEVEL = '00002a19-0000-1000-8000-00805f9b34fb'


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeChar:
    def __init__(self, uuid, handle, properties, mtu=None):
        self.uuid = uuid
        self.handle = handle
        self.properties = properties
        self.description = 'char %d' % handle
        self.obj = {} if mtu is None else {'MTU': mtu}


class FakeService:
    def __init__(self, uuid, characteristics):
        self.uuid = uuid
        self.description = 'service'
        self.characteristics = characteristics


def default_services(mtu=247):
    return [
        FakeService(SVC, [
            FakeChar(NOTIFY, 10, ['notify'], mtu),
            FakeChar(WRITE, 12, ['write', 'write-without-response'], mtu),
            FakeChar(READ, 14, ['read'], mtu),
            FakeChar(DUP, 16, ['read'], mtu),
            FakeChar(BATTERY_LEVEL, 18, ['read', 'notify'], mtu),
        ]),
        FakeService('0000180a-0000-1000-8000-00805f9b34fb', [
            FakeChar(DUP, 20, ['read'], mtu),
        ]),
    ]


class BleakDeviceNotFoundError(Exception):
    """Matched by class name, like the real bleak.exc class."""


class FakeClient:
    """Scripted stand-in for bleak.BleakClient."""

    def __init__(self, address, timeout, disconnected_callback, *,
                 services=None, connect_exc=None, notify_on_subscribe=None,
                 write_delay=0.0, write_exc=None):
        self.address = address
        self.timeout = timeout
        self._disconnected_callback = disconnected_callback
        self.services = services if services is not None else default_services()
        self.connect_exc = connect_exc
        self.notify_on_subscribe = notify_on_subscribe
        self.write_delay = write_delay
        self.write_exc = write_exc
        self.callbacks = {}
        self.log = []  # ordered record of GATT operations
        self.disconnected = False
        self.loop = None

    async def connect(self):
        self.loop = asyncio.get_running_loop()
        if self.connect_exc is not None:
            raise self.connect_exc
        return True

    async def start_notify(self, char, callback):
        self.callbacks[char.handle] = (char, callback)
        self.log.append(('cccd', char.handle))
        if self.notify_on_subscribe is not None:
            callback(char, bytearray(self.notify_on_subscribe))

    async def stop_notify(self, char):
        self.callbacks.pop(char.handle, None)
        self.log.append(('stop', char.handle))

    async def write_gatt_char(self, char, data, response=None):
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        if self.write_exc is not None:
            raise self.write_exc
        self.log.append(('write', char.handle, bytes(data), response))

    async def read_gatt_char(self, char):
        self.log.append(('read', char.handle))
        return bytearray(b'\x01\x02')

    async def disconnect(self):
        self.disconnected = True
        return True

    # -- test drivers (any thread) --
    def push(self, handle, data):
        char, cb = self.callbacks[handle]
        self.loop.call_soon_threadsafe(cb, char, bytearray(data))

    def drop_link(self):
        self.loop.call_soon_threadsafe(self._disconnected_callback, self)


class Recorder:
    """Collects emitted (event, payload) pairs."""

    def __init__(self):
        self.events = []
        self.cv = threading.Condition()

    def emit(self, event, payload):
        with self.cv:
            self.events.append((event, payload))
            self.cv.notify_all()

    def wait_for(self, pred, timeout=3.0):
        deadline = time.monotonic() + timeout
        with self.cv:
            while True:
                for i, (event, payload) in enumerate(self.events):
                    if pred(event, payload):
                        return i, payload
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError('event not seen; got %r' % self.events)
                self.cv.wait(remaining)

    def result(self, seq, timeout=3.0):
        return self.wait_for(
            lambda e, p: e == 'ble_result' and p.get('seq') == seq, timeout)[1]

    def closed(self, timeout=3.0):
        return self.wait_for(lambda e, p: e == 'ble_closed', timeout)[1]

    def notifications(self):
        with self.cv:
            return [item for e, p in self.events if e == 'ble_notify'
                    for item in p['items']]

    def index(self, pred):
        return self.wait_for(pred)[0]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class SessionTestBase(unittest.TestCase):

    def setUp(self):
        self.clients = []
        self.client_kwargs = {}
        # Per-client connect failures, consumed in order (None = connects).
        self.connect_exc_queue = []
        self.gone = False
        self._sid_n = 0
        patcher = patch.object(ble_session, '_make_client', self._make_client)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (('WATCHDOG_INTERVAL', 0.05), ('SEQ_GAP_TIMEOUT', 0.3)):
            p = patch.object(ble_session, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._teardown_channels)

    def _make_client(self, address, timeout, disconnected_callback):
        kwargs = dict(self.client_kwargs)
        if self.connect_exc_queue:
            kwargs['connect_exc'] = self.connect_exc_queue.pop(0)
        client = FakeClient(address, timeout, disconnected_callback, **kwargs)
        self.clients.append(client)
        return client

    def _teardown_channels(self):
        with ble_session._channels_lock:
            channels = list(ble_session._channels.values())
        for channel in channels:
            ble_session.run_bleak(channel.destroy(), 10)
        # Nothing may leak the adapter into the next test.
        self.assertTrue(ble_handler.bt_adapter_lock.acquire(timeout=1))
        ble_handler.bt_adapter_lock.release()
        self.assertIsNone(ble_handler.adapter_holder_info())

    def channel(self):
        self._sid_n += 1
        rec = Recorder()
        ch = ble_session.get_channel('sid-%d-%s' % (self._sid_n, id(self)),
                                     emit=rec.emit, client_gone=lambda: self.gone)
        ch.rec = rec
        ch.seq = 0
        return ch

    def send(self, ch, op, **fields):
        ch.seq += 1
        ch.submit(op, dict(fields, seq=ch.seq))
        return ch.seq

    def call(self, ch, op, **fields):
        return ch.rec.result(self.send(ch, op, **fields))

    def opened(self, **fields):
        ch = self.channel()
        r = self.call(ch, 'open', address=ADDR, **fields)
        self.assertTrue(r['ok'], r)
        return ch, r['value']


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOpen(SessionTestBase):

    def test_open_reports_bluez_mtu_and_services_with_handles(self):
        _ch, value = self.opened()
        self.assertEqual(value['address'], ADDR)
        self.assertEqual(value['mtu'], 247)
        self.assertEqual(value['mtu_source'], 'bluez')
        chars = value['services'][0]['characteristics']
        self.assertEqual(chars[0], {'uuid': NOTIFY, 'handle': 10,
                                    'description': 'char 10',
                                    'properties': ['notify']})

    def test_open_without_mtu_property_reports_default(self):
        self.client_kwargs['services'] = default_services(mtu=None)
        _ch, value = self.opened()
        self.assertEqual((value['mtu'], value['mtu_source']), (23, 'default'))

    def test_open_passes_connect_timeout_to_bleak(self):
        self.opened(connect_timeout=7)
        self.assertEqual(self.clients[0].timeout, 7.0)

    def test_device_not_found(self):
        self.client_kwargs['connect_exc'] = BleakDeviceNotFoundError(ADDR)
        ch = self.channel()
        r = self.call(ch, 'open', address=ADDR)
        self.assertEqual(r['code'], 'device_not_found')
        self.assertIn('lager ble scan', r['message'])
        self.assertEqual(ch.state, 'idle')

    def test_device_lost_between_scan_and_connect_is_retried_once(self):
        # bleak's plain BleakError when BlueZ drops the device after the scan.
        self.connect_exc_queue = [RuntimeError("device 'dev_AA_BB_CC_DD_EE_01' not found"),
                                  None]
        _ch, value = self.opened()
        self.assertEqual(value['mtu'], 247)
        self.assertEqual(len(self.clients), 2)

    def test_device_lost_twice_is_device_not_found(self):
        lost = RuntimeError("device 'dev_AA_BB_CC_DD_EE_01' not found")
        self.connect_exc_queue = [lost, lost]
        r = self.call(self.channel(), 'open', address=ADDR)
        self.assertEqual(r['code'], 'device_not_found')
        self.assertEqual(len(self.clients), 2)
        self.assertIsNone(ble_handler.adapter_holder_info())

    def test_connect_failed(self):
        self.client_kwargs['connect_exc'] = RuntimeError('le-connection-abort-by-local')
        r = self.call(self.channel(), 'open', address=ADDR)
        self.assertEqual(r['code'], 'connect_failed')
        self.assertIn('le-connection-abort-by-local', r['message'])

    def test_bluez_missing_uses_the_remedy_message(self):
        self.client_kwargs['connect_exc'] = RuntimeError(
            '[org.freedesktop.DBus.Error.ServiceUnknown] The name org.bluez was '
            'not provided by any .service files')
        r = self.call(self.channel(), 'open', address=ADDR)
        self.assertEqual(r['code'], 'bluez_unavailable')
        self.assertEqual(r['message'], ble_handler.BLUEZ_UNAVAILABLE_MESSAGE)

    def test_invalid_address_and_timeouts(self):
        ch = self.channel()
        self.assertEqual(self.call(ch, 'open', address='nope')['code'], 'invalid_argument')
        self.assertEqual(self.call(ch, 'open', address=ADDR, idle_timeout=1)['code'],
                         'invalid_argument')
        self.assertEqual(self.call(ch, 'open', address=ADDR, connect_timeout='x')['code'],
                         'invalid_argument')

    def test_second_open_on_same_connection(self):
        ch, _ = self.opened()
        self.assertEqual(self.call(ch, 'open', address=ADDR)['code'], 'session_active')

    def test_ops_before_open_are_not_open(self):
        self.assertEqual(self.call(self.channel(), 'read', char=READ)['code'], 'not_open')

    def test_reopen_after_close(self):
        ch, _ = self.opened()
        self.assertTrue(self.call(ch, 'close')['ok'])
        self.assertTrue(self.call(ch, 'open', address=ADDR)['ok'])


class TestOrdering(SessionTestBase):

    def test_out_of_order_seq_runs_in_order(self):
        ch, _ = self.opened()  # seq 1
        ch.submit('write', {'seq': 4, 'char': WRITE, 'data': '03'})
        ch.submit('write', {'seq': 3, 'char': WRITE, 'data': '02'})
        time.sleep(0.05)
        ch.submit('write', {'seq': 2, 'char': WRITE, 'data': '01'})
        ch.rec.result(4)
        writes = [e[2] for e in self.clients[0].log if e[0] == 'write']
        self.assertEqual(writes, [b'\x01', b'\x02', b'\x03'])

    def test_gap_that_never_fills_is_protocol_error(self):
        ch, _ = self.opened()
        ch.submit('ping', {'seq': 3})
        r = ch.rec.result(3)
        self.assertEqual(r['code'], 'protocol_error')
        self.assertIn('seq 2 never arrived', r['message'])
        self.assertEqual(ch.rec.closed()['reason'], 'protocol_error')

    def test_reused_or_bad_seq_is_refused(self):
        ch, _ = self.opened()
        ch.submit('ping', {'seq': 1})
        self.assertEqual(ch.rec.wait_for(
            lambda e, p: e == 'ble_result' and p.get('code') == 'protocol_error')[1]['seq'], 1)
        ch.submit('ping', {'seq': 'two'})
        ch.rec.wait_for(lambda e, p: e == 'ble_result' and p.get('code') == 'invalid_argument')


class TestNotifications(SessionTestBase):

    def test_subscribe_writes_cccd_and_streams_notifications(self):
        ch, _ = self.opened()
        r = self.call(ch, 'subscribe', char=NOTIFY)
        self.assertEqual(r['value'], {'char': NOTIFY, 'handle': 10, 'mode': 'notify'})
        client = self.clients[0]
        self.assertEqual(client.log, [('cccd', 10)])
        before = time.time()
        client.push(10, b'\xaa\xbb')
        client.push(10, b'\xcc')
        ch.rec.wait_for(lambda e, p: len(ch.rec.notifications()) >= 2)
        items = ch.rec.notifications()
        self.assertEqual([i['data'] for i in items], ['aabb', 'cc'])
        self.assertEqual([i['n'] for i in items], [1, 2])
        self.assertEqual({i['char'] for i in items}, {NOTIFY})
        self.assertEqual({i['handle'] for i in items}, {10})
        self.assertGreaterEqual(items[0]['ts'], before - 1)

    def test_notification_during_cccd_write_is_delivered_before_result(self):
        self.client_kwargs['notify_on_subscribe'] = b'\x01'
        ch, _ = self.opened()
        seq = self.send(ch, 'subscribe', char=NOTIFY)
        result_at = ch.rec.index(lambda e, p: e == 'ble_result' and p['seq'] == seq)
        notify_at = ch.rec.index(lambda e, p: e == 'ble_notify')
        self.assertLess(notify_at, result_at)

    def test_subscribe_needs_notify_or_indicate(self):
        ch, _ = self.opened()
        r = self.call(ch, 'subscribe', char=READ)
        self.assertEqual(r['code'], 'not_permitted')
        self.assertIn('properties: read', r['message'])

    def test_indicate_only_characteristic(self):
        self.client_kwargs['services'] = [FakeService(SVC, [
            FakeChar(NOTIFY, 10, ['indicate'], 247)])]
        ch, _ = self.opened()
        self.assertEqual(self.call(ch, 'subscribe', char=NOTIFY)['value']['mode'], 'indicate')

    def test_unsubscribe(self):
        ch, _ = self.opened()
        self.call(ch, 'subscribe', char=NOTIFY)
        self.assertTrue(self.call(ch, 'unsubscribe', char=NOTIFY)['ok'])
        self.assertEqual(self.clients[0].log, [('cccd', 10), ('stop', 10)])

    def test_overflow_ends_the_session(self):
        ch, _ = self.opened()
        self.call(ch, 'subscribe', char=NOTIFY)
        with patch.object(ble_session, 'NOTIFY_QUEUE_MAX_ITEMS', 0):
            self.clients[0].push(10, b'\x01')
            closed = ch.rec.closed()
        self.assertEqual(closed['reason'], 'overflow')
        self.assertEqual(ch.rec.notifications(), [])


class TestWritesAndReads(SessionTestBase):

    def test_write_with_and_without_response(self):
        ch, _ = self.opened()
        r = self.call(ch, 'write', char=WRITE, data='0102')
        self.assertEqual(r['value'], {'char': WRITE, 'handle': 12, 'bytes': 2, 'chunks': 1})
        self.call(ch, 'write', char=WRITE, data='03', response=False)
        self.assertEqual(self.clients[0].log, [
            ('write', 12, b'\x01\x02', True),
            ('write', 12, b'\x03', False),
        ])

    def test_chunked_write_splits_to_mtu_minus_3_in_order(self):
        self.client_kwargs['services'] = default_services(mtu=23)
        ch, _ = self.opened()
        payload = bytes(range(50))
        r = self.call(ch, 'write', char=WRITE, data=payload.hex(), chunk=True)
        self.assertEqual(r['value']['chunks'], 3)
        pieces = [e[2] for e in self.clients[0].log if e[0] == 'write']
        self.assertEqual([len(p) for p in pieces], [20, 20, 10])
        self.assertEqual(b''.join(pieces), payload)

    def test_chunks_never_exceed_the_512_byte_att_limit(self):
        # MTU 517 would allow 514-byte writes; no attribute value is over 512.
        self.client_kwargs['services'] = default_services(mtu=517)
        ch, _ = self.opened()
        payload = bytes(i % 256 for i in range(600))
        r = self.call(ch, 'write', char=WRITE, data=payload.hex(), chunk=True)
        self.assertEqual(r['value']['chunks'], 2)
        pieces = [e[2] for e in self.clients[0].log if e[0] == 'write']
        self.assertEqual([len(p) for p in pieces], [512, 88])
        self.assertEqual(b''.join(pieces), payload)

    def test_chunk_size_caps_the_pieces(self):
        self.client_kwargs['services'] = default_services(mtu=517)
        ch, _ = self.opened()
        payload = bytes(range(250))
        r = self.call(ch, 'write', char=WRITE, data=payload.hex(), chunk_size=100)
        self.assertEqual(r['value']['chunks'], 3)
        pieces = [e[2] for e in self.clients[0].log if e[0] == 'write']
        self.assertEqual([len(p) for p in pieces], [100, 100, 50])
        self.assertEqual(b''.join(pieces), payload)

    def test_chunk_size_never_exceeds_what_the_mtu_allows(self):
        self.client_kwargs['services'] = default_services(mtu=23)
        ch, _ = self.opened()
        self.call(ch, 'write', char=WRITE, data='00' * 50, chunk=True, chunk_size=500)
        pieces = [e[2] for e in self.clients[0].log if e[0] == 'write']
        self.assertEqual([len(p) for p in pieces], [20, 20, 10])

    def test_bad_chunk_size(self):
        ch, _ = self.opened()
        for bad in (0, 513, 'big', True):
            r = self.call(ch, 'write', char=WRITE, data='00', chunk_size=bad)
            self.assertEqual(r['code'], 'invalid_argument', bad)
        self.assertEqual(self.clients[0].log, [])

    def test_unchunked_write_over_512_bytes_is_refused(self):
        self.client_kwargs['services'] = default_services(mtu=517)
        ch, _ = self.opened()
        r = self.call(ch, 'write', char=WRITE, data='00' * 513)
        self.assertEqual(r['code'], 'invalid_argument')
        self.assertIn('chunk', r['message'])
        self.assertEqual(self.clients[0].log, [])
        self.assertTrue(self.call(ch, 'write', char=WRITE, data='00' * 512)['ok'])

    def test_write_property_checks(self):
        self.client_kwargs['services'] = [FakeService(SVC, [
            FakeChar(WRITE, 12, ['write-without-response'], 247)])]
        ch, _ = self.opened()
        r = self.call(ch, 'write', char=WRITE, data='01')
        self.assertEqual(r['code'], 'not_permitted')
        self.assertTrue(self.call(ch, 'write', char=WRITE, data='01', response=False)['ok'])

    def test_write_rejects_bad_hex_and_oversize(self):
        ch, _ = self.opened()
        self.assertEqual(self.call(ch, 'write', char=WRITE, data='zz')['code'],
                         'invalid_argument')
        big = '00' * (ble_session.MAX_WRITE_BYTES + 1)
        self.assertEqual(self.call(ch, 'write', char=WRITE, data=big)['code'],
                         'invalid_argument')

    def test_peripheral_refusal_is_not_permitted(self):
        exc = RuntimeError('[org.bluez.Error.NotPermitted] Write not permitted')
        self.client_kwargs['write_exc'] = exc
        ch, _ = self.opened()
        self.assertEqual(self.call(ch, 'write', char=WRITE, data='01')['code'],
                         'not_permitted')
        self.assertEqual(ch.state, 'open')

    def test_read(self):
        ch, _ = self.opened()
        self.assertEqual(self.call(ch, 'read', char=READ)['value']['data'], '0102')

    def test_info_and_ping(self):
        ch, opened = self.opened(idle_timeout=60)
        self.assertEqual(self.call(ch, 'info')['value'], opened)
        self.assertEqual(self.call(ch, 'ping')['value']['idle_timeout'], 60.0)


class TestCharacteristicAddressing(SessionTestBase):

    def test_unknown(self):
        ch, _ = self.opened()
        r = self.call(ch, 'read', char='12345678-0000-0000-0000-000000000000')
        self.assertEqual(r['code'], 'unknown_characteristic')
        self.assertEqual(self.call(ch, 'read', handle=99)['code'], 'unknown_characteristic')

    def test_ambiguous_uuid_lists_handles_and_handle_resolves_it(self):
        ch, _ = self.opened()
        r = self.call(ch, 'read', char=DUP)
        self.assertEqual(r['code'], 'ambiguous_characteristic')
        self.assertIn('16, 20', r['message'])
        self.assertEqual(self.call(ch, 'read', handle=20)['value']['handle'], 20)

    def test_short_and_uppercase_uuids(self):
        ch, _ = self.opened()
        self.assertEqual(self.call(ch, 'read', char='2A19')['value']['handle'], 18)
        self.assertEqual(self.call(ch, 'read', char=READ.upper())['value']['handle'], 14)

    def test_normalize_uuid(self):
        self.assertEqual(ble_session.normalize_uuid('0x180F'),
                         '0000180f-0000-1000-8000-00805f9b34fb')
        self.assertEqual(ble_session.normalize_uuid('0000180F'),
                         '0000180f-0000-1000-8000-00805f9b34fb')


class TestLifecycle(SessionTestBase):

    def test_every_event_carries_an_increasing_e(self):
        ch, _ = self.opened()
        self.call(ch, 'subscribe', char=NOTIFY)
        self.clients[0].push(10, b'\x01')
        self.call(ch, 'close')
        ch.rec.closed()
        es = [p['e'] for _, p in ch.rec.events]
        self.assertEqual(es, list(range(1, len(es) + 1)))

    def test_close_sends_result_then_closed_and_disconnects(self):
        ch, _ = self.opened()
        seq = self.send(ch, 'close')
        result_at = ch.rec.index(lambda e, p: e == 'ble_result' and p['seq'] == seq)
        closed_at = ch.rec.index(lambda e, p: e == 'ble_closed')
        self.assertLess(result_at, closed_at)
        self.assertEqual(ch.rec.closed()['reason'], 'client')
        self.assertTrue(self.clients[0].disconnected)

    def test_link_loss_flushes_notifications_then_closes(self):
        ch, _ = self.opened()
        self.call(ch, 'subscribe', char=NOTIFY)
        client = self.clients[0]
        client.push(10, b'\x01')
        client.drop_link()
        closed_at = ch.rec.index(lambda e, p: e == 'ble_closed')
        notify_at = ch.rec.index(lambda e, p: e == 'ble_notify')
        self.assertLess(notify_at, closed_at)
        self.assertEqual(ch.rec.closed()['reason'], 'disconnected')
        self.assertEqual(self.call(ch, 'read', char=READ)['code'], 'not_open')

    def test_link_loss_during_an_operation_fails_it_before_closing(self):
        self.client_kwargs['write_delay'] = 0.3
        ch, _ = self.opened()
        seq = self.send(ch, 'write', char=WRITE, data='01')
        time.sleep(0.1)
        client = self.clients[0]
        client.write_exc = RuntimeError('Not connected')
        client.drop_link()
        r = ch.rec.result(seq)
        self.assertEqual(r['code'], 'disconnected')
        result_at = ch.rec.index(lambda e, p: e == 'ble_result' and p['seq'] == seq)
        closed_at = ch.rec.index(lambda e, p: e == 'ble_closed')
        self.assertLess(result_at, closed_at)

    def test_idle_timeout_ignores_notifications_but_not_ping(self):
        ch, _ = self.opened()
        self.call(ch, 'subscribe', char=NOTIFY)
        ch.idle_timeout = 0.4  # below the wire minimum, for test speed
        for _ in range(3):
            time.sleep(0.2)
            self.call(ch, 'ping')
        self.assertEqual(ch.state, 'open')
        for _ in range(8):
            self.clients[0].push(10, b'\x00')
            time.sleep(0.1)
        self.assertEqual(ch.rec.closed()['reason'], 'idle_timeout')

    def test_client_gone_destroys_the_channel(self):
        ch, _ = self.opened()
        self.gone = True
        ch.rec.closed()
        self.assertNotIn(ch.sid, ble_session._channels)
        self.assertTrue(self.clients[0].disconnected)

    def test_drop_channel_on_socket_disconnect(self):
        ch, _ = self.opened()
        ble_session.drop_channel(ch.sid)
        self.assertEqual(ch.rec.closed()['reason'], 'client')
        self.assertNotIn(ch.sid, ble_session._channels)


class TestAdapterOwnership(SessionTestBase):

    def setUp(self):
        super().setUp()
        app = Flask(__name__)
        ble_handler.register_ble_routes(app)
        blufi_handler.register_blufi_routes(app)
        ble_session.register_ble_session_routes(app)
        self.http = app.test_client()

    def test_one_shot_requests_get_409_while_a_session_is_open(self):
        self.opened(holder='alice@example.com')
        r = self.http.post('/ble/command', json={'action': 'scan', 'params': {}})
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body['code'], 'adapter_busy')
        self.assertIn(ADDR, body['error'])
        self.assertIn('alice@example.com', body['error'])
        self.assertIn('lager ble sessions --release', body['error'])
        r = self.http.post('/blufi/command', json={'action': 'scan', 'params': {}})
        self.assertEqual(r.status_code, 409)

    def test_second_session_is_adapter_busy(self):
        self.opened()
        r = self.call(self.channel(), 'open', address='AA:BB:CC:DD:EE:02')
        self.assertEqual(r['code'], 'adapter_busy')

    def test_adapter_is_free_after_close(self):
        ch, _ = self.opened()
        self.call(ch, 'close')
        ch.rec.closed()
        with patch.object(ble_handler, 'run_bleak', lambda coro, t: (coro.close(), [])[1]):
            r = self.http.post('/ble/command', json={'action': 'scan', 'params': {}})
        self.assertEqual(r.status_code, 200)

    def test_failed_open_frees_the_adapter(self):
        self.client_kwargs['connect_exc'] = RuntimeError('boom')
        self.call(self.channel(), 'open', address=ADDR)
        self.assertIsNone(ble_handler.adapter_holder_info())

    def test_sessions_list_and_release(self):
        ch, _ = self.opened(holder='ci-runner')
        self.call(ch, 'subscribe', char=NOTIFY)
        body = self.http.get('/ble/sessions').get_json()
        self.assertEqual(len(body['sessions']), 1)
        s = body['sessions'][0]
        self.assertEqual((s['address'], s['holder'], s['mtu']), (ADDR, 'ci-runner', 247))
        self.assertEqual(s['subscriptions'], [NOTIFY])

        r = self.http.post('/ble/sessions/release', json={'address': 'AA:BB:CC:DD:EE:09'})
        self.assertEqual(r.status_code, 404)
        r = self.http.post('/ble/sessions/release', json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['released'][0]['address'], ADDR)
        self.assertEqual(ch.rec.closed()['reason'], 'released')
        self.assertEqual(self.http.get('/ble/sessions').get_json()['sessions'], [])


class TestBleTarget(unittest.TestCase):
    """ble.ble_target hands bleak BlueZ's own record when it has one."""

    def _run(self, properties, adapter='/org/bluez/hci0'):
        class Manager:
            _properties = properties

            def get_default_adapter(self):
                return adapter

        async def get_manager():
            return Manager()

        class BLEDevice:
            def __init__(self, address, name, details, rssi):
                self.address, self.name, self.details, self.rssi = address, name, details, rssi

        manager_mod = types.ModuleType('bleak.backends.bluezdbus.manager')
        manager_mod.get_global_bluez_manager = get_manager
        device_mod = types.ModuleType('bleak.backends.device')
        device_mod.BLEDevice = BLEDevice
        with patch.dict(sys.modules, {'bleak.backends.bluezdbus.manager': manager_mod,
                                      'bleak.backends.device': device_mod}):
            return asyncio.run(ble_handler.ble_target('aa:bb:cc:dd:ee:01'))

    def test_known_device_is_connected_directly(self):
        path = '/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01'
        props = {'Address': ADDR, 'Alias': 'peer', 'RSSI': -40}
        target = self._run({path: {'org.bluez.Device1': props}})
        self.assertEqual(target.address, ADDR)
        self.assertEqual(target.details, {'path': path, 'props': props})

    def test_unknown_device_falls_back_to_the_address(self):
        self.assertEqual(self._run({}), 'aa:bb:cc:dd:ee:01')

    def test_lookup_failure_falls_back_to_the_address(self):
        # The stubbed bleak in this suite cannot be awaited at all.
        self.assertEqual(asyncio.run(ble_handler.ble_target(ADDR)), ADDR)


class TestClassifyError(unittest.TestCase):

    def test_codes(self):
        c = ble_session.classify_error
        self.assertEqual(c(RuntimeError('x'), link_lost=True)[0], 'disconnected')
        self.assertEqual(c(RuntimeError('Not connected'))[0], 'disconnected')
        exc = RuntimeError('boom')
        exc.dbus_error = 'org.bluez.Error.NotAuthorized'
        self.assertEqual(c(exc)[0], 'not_permitted')
        self.assertEqual(c(RuntimeError('weird'))[0], 'ble_error')


if __name__ == '__main__':
    unittest.main()
