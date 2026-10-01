# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for BLE GATT sessions in the CLI.

``BLESessionClient`` (cli/commands/communication/ble_session_client.py) is
driven through a fake socketio.Client that answers each request the way the
box's /ble namespace does. The ``lager ble session`` / ``lager ble sessions``
commands run under CliRunner with box resolution, the lock holder and the
HTTP calls patched. Nothing opens a socket.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

client_mod = importlib.import_module('cli.commands.communication.ble_session_client')
ble_cmd = importlib.import_module('cli.commands.communication.ble')
net_helpers = importlib.import_module('cli.core.net_helpers')

ADDR = 'AA:BB:CC:DD:EE:01'
NOTIFY = '12345678-1234-5678-1234-56789abcdef1'
WRITE = '12345678-1234-5678-1234-56789abcdef2'
READ = '12345678-1234-5678-1234-56789abcdef3'

OPENED = {'address': ADDR, 'mtu': 247, 'mtu_source': 'bluez', 'services': [
    {'uuid': '12345678-1234-5678-1234-56789abcdef0', 'description': 's',
     'characteristics': [{'uuid': NOTIFY, 'handle': 10, 'description': 'n',
                          'properties': ['notify']}]}]}


class FakeBox:
    """A socketio.Client stand-in that plays the box's side of /ble.

    ``replies`` maps an op ('open', 'write', ...) to a callable taking the
    request fields and returning a list of box events to deliver, in order,
    before emit() returns. The default answers every op with ok + a canned
    value.
    """

    def __init__(self, replies=None):
        self.handlers = {}
        self.sent = []
        self.replies = replies or {}
        self.disconnects = 0

    # socketio.Client surface
    def on(self, event, handler=None, namespace=None):
        self.handlers[event] = handler

    def connect(self, url, namespaces=None, wait_timeout=None, headers=None):
        self.url = url

    def disconnect(self):
        self.disconnects += 1
        self.handlers['disconnect']()

    def emit(self, event, data=None, namespace=None):
        assert namespace == '/ble'
        self.sent.append((event, dict(data)))
        op = event[len('ble_'):]
        reply = self.replies.get(op, self.default)
        for name, payload in reply(data):
            if name == 'ble_result':
                payload = dict(payload, seq=data['seq'])
            self.handlers[name](payload)

    # helpers
    @staticmethod
    def ok(value=None):
        return [('ble_result', {'ok': True, 'value': value or {}})]

    @staticmethod
    def err(code, message):
        return [('ble_result', {'ok': False, 'code': code, 'message': message})]

    @staticmethod
    def notify(*items):
        return ('ble_notify', {'items': [
            {'n': n, 'char': NOTIFY, 'handle': 10, 'ts': 1700000000.25, 'data': data}
            for n, data in items]})

    def default(self, data):
        op = self.sent[-1][0]
        if op == 'ble_open':
            return self.ok(OPENED)
        if op == 'ble_subscribe':
            return self.ok({'char': NOTIFY, 'handle': 10, 'mode': 'notify'})
        if op == 'ble_write':
            n = len(bytes.fromhex(data['data']))
            return self.ok({'char': WRITE, 'handle': 12, 'bytes': n,
                            'chunks': 3 if data.get('chunk') else 1})
        if op == 'ble_read':
            return self.ok({'char': READ, 'handle': 14, 'data': '0102'})
        if op == 'ble_close':
            return self.ok() + [('ble_closed', {'reason': 'client', 'message': ''})]
        return self.ok()


def make_client(replies=None):
    fake = FakeBox(replies)
    client = client_mod.BLESessionClient('http://box:9000', sio=fake, op_timeout=1.0)
    client.connect()
    return client, fake


# ---------------------------------------------------------------------------
# BLESessionClient
# ---------------------------------------------------------------------------

def test_requests_carry_increasing_seq_and_fields():
    client, fake = make_client()
    assert client.open(ADDR, holder='me') == OPENED
    client.subscribe(NOTIFY)
    client.write(WRITE, b'\x01\x02', response=False, chunk=True)
    client.write(None, b'', handle=12)
    assert client.read(READ) == b'\x01\x02'
    assert [(e, d['seq']) for e, d in fake.sent] == [
        ('ble_open', 1), ('ble_subscribe', 2), ('ble_write', 3),
        ('ble_write', 4), ('ble_read', 5)]
    assert fake.sent[0][1] == {'seq': 1, 'address': ADDR, 'connect_timeout': 10.0,
                               'idle_timeout': 300.0, 'holder': 'me'}
    assert fake.sent[2][1] == {'seq': 3, 'char': WRITE, 'data': '0102',
                               'response': False, 'chunk': True}
    assert fake.sent[3][1] == {'seq': 4, 'handle': 12, 'data': '',
                               'response': True, 'chunk': False}


def test_chunked_write_waits_in_proportion_to_its_chunks():
    client, _ = make_client()
    client.open(ADDR)  # mtu 247 -> 244-byte chunks; op_timeout 1s
    assert client.write_timeout(60000, chunk=False) == 1.0
    assert client.write_timeout(1, chunk=True) == 15.0
    assert client.write_timeout(600, chunk=True) == 35.0
    client.mtu = 517  # 514 would exceed the 512-byte ATT limit
    assert client.write_timeout(1024, chunk=True) == 25.0


def test_chunk_size_is_sent_and_turns_chunking_on():
    client, fake = make_client()
    client.open(ADDR)
    client.write(WRITE, b'\x00' * 250, chunk_size=100)
    assert fake.sent[-1][1]['chunk_size'] == 100
    assert fake.sent[-1][1]['chunk'] is True
    assert client.write_timeout(250, True, 100) == 35.0  # 3 chunks
    client.write(WRITE, b'\x00')
    assert 'chunk_size' not in fake.sent[-1][1]


def test_chunk_size_option():
    result, fake = run_session(['--write', f'{WRITE}:00', '--chunk-size', '100',
                                '--listen', '0'])
    assert result.exit_code == 0, result.output
    write = next(d for e, d in fake.sent if e == 'ble_write')
    assert (write['chunk_size'], write['chunk']) == (100, True)
    result, _ = run_session(['--write', f'{WRITE}:00', '--chunk-size', '0', '--listen', '0'])
    assert result.exit_code == 2


def test_holder_is_omitted_when_unset():
    client, fake = make_client()
    client.open(ADDR)
    assert 'holder' not in fake.sent[0][1]


def test_error_result_raises_with_code():
    client, _ = make_client({'open': lambda d: FakeBox.err('adapter_busy', 'busy')})
    with pytest.raises(client_mod.BLESessionError) as info:
        client.open(ADDR)
    assert (info.value.code, info.value.message) == ('adapter_busy', 'busy')


def test_notifications_are_decoded_and_queued_in_order():
    client, _ = make_client({'subscribe': lambda d: [
        FakeBox.notify((1, 'aa'), (2, 'zz'), (3, 'bbcc'))] + FakeBox.ok({'char': NOTIFY})})
    client.open(ADDR)
    client.subscribe(NOTIFY)
    first = client.get_notification()
    assert (first['n'], first['data'], first['char']) == (1, b'\xaa', NOTIFY)
    # The item with invalid hex is skipped, not delivered corrupt.
    assert client.get_notification()['data'] == b'\xbb\xcc'
    assert client.get_notification() is None


def test_closed_event_and_lost_connection():
    client, fake = make_client({'ping': lambda d: [
        ('ble_closed', {'reason': 'disconnected', 'message': 'gone'})]})
    client.open(ADDR)
    with pytest.raises(client_mod.BLESessionError):
        client.ping()  # no ble_result: times out after op_timeout
    assert client.closed == {'reason': 'disconnected', 'message': 'gone'}

    client2, fake2 = make_client({'read': lambda d: (fake2.disconnect(), [])[1]})
    client2.open(ADDR)
    with pytest.raises(client_mod.BLESessionClosed) as info:
        client2.read(READ)
    assert info.value.code == 'connection_lost'
    assert client2.closed['reason'] == 'connection_lost'


def test_events_run_in_box_order_whatever_order_they_arrive_in():
    # python-engineio hands each message to its own thread; the box's `e`
    # counter puts them back in order.
    client, fake = make_client()
    notify = fake.handlers['ble_notify']
    closed = fake.handlers['ble_closed']
    batch = lambda e, *items: {'e': e, 'items': [
        {'n': n, 'char': NOTIFY, 'handle': 10, 'ts': 0.0, 'data': d} for n, d in items]}
    closed({'e': 4, 'reason': 'disconnected', 'message': 'gone'})
    notify(batch(3, (3, '03')))
    assert client.get_notification() is None and client.closed is None  # held
    notify(batch(1, (1, '01')))
    assert client.get_notification()['n'] == 1  # e=1 runs; e=2 still missing
    assert client.get_notification() is None
    notify(batch(2, (2, '02')))
    assert [client.get_notification()['n'] for _ in range(2)] == [2, 3]
    assert client.closed == {'reason': 'disconnected', 'message': 'gone'}


def test_close_waits_for_ble_closed():
    client, fake = make_client()
    client.open(ADDR)
    client.close()
    assert fake.sent[-1][0] == 'ble_close'
    assert client.closed == {'reason': 'client', 'message': ''}
    client.close()  # idempotent: nothing more is sent
    assert len(fake.sent) == 2


# ---------------------------------------------------------------------------
# lager ble session / sessions
# ---------------------------------------------------------------------------

def run_session(args, replies=None, tty=False):
    fake_holder = {}

    def connect(box_ip):
        client, fake = make_client(replies)
        fake_holder['fake'] = fake
        return client

    with patch.object(ble_cmd, 'resolve_box_locked', return_value='192.0.2.4'), \
            patch.object(ble_cmd, '_connect_session_client', side_effect=connect), \
            patch('cli.box_storage.get_lock_holder', return_value='tester'), \
            patch.object(ble_cmd.sys.stdin, 'isatty', return_value=tty), \
            patch.object(ble_cmd.sys.stdout, 'isatty', return_value=tty):
        result = CliRunner().invoke(ble_cmd.ble, ['session', ADDR] + args)
    return result, fake_holder.get('fake')


def test_one_shot_order_and_json_output():
    replies = {'write': lambda d: [FakeBox.notify((1, '0a'))] + FakeBox.ok(
        {'char': WRITE, 'handle': 12, 'bytes': 1, 'chunks': 1})}
    result, fake = run_session(
        ['--subscribe', NOTIFY, '--write', f'{WRITE}:01', '--write', f'{WRITE}:02',
         '--read', READ, '--listen', '0', '--json', '--chunk'], replies)
    assert result.exit_code == 0, result.output
    assert [e for e, _ in fake.sent] == [
        'ble_open', 'ble_subscribe', 'ble_write', 'ble_write', 'ble_read', 'ble_close']
    assert fake.sent[0][1]['holder'] == 'tester'
    assert fake.sent[2][1]['chunk'] is True
    lines = [json.loads(line) for line in result.output.splitlines()]
    events = [line['event'] for line in lines]
    assert events[:5] == ['open', 'subscribe', 'write', 'write', 'read']
    assert events.count('notify') == 2
    assert events[-1] == 'closed'
    notify = next(line for line in lines if line['event'] == 'notify')
    assert notify == {'event': 'notify', 'n': 1, 'char': NOTIFY, 'handle': 10,
                      'ts': 1700000000.25, 'data': '0a'}


def test_human_output_reports_mtu_and_latency():
    replies = {'write': lambda d: FakeBox.ok(
        {'char': WRITE, 'handle': 12, 'bytes': 1, 'chunks': 1}) + [FakeBox.notify((1, 'ff'))]}
    result, _ = run_session(['--write', f'{WRITE}:01', '--listen', '0.3'], replies)
    assert result.exit_code == 0, result.output
    assert 'MTU 247 (negotiated)' in result.output
    assert f'#1 {NOTIFY} ff' in result.output
    assert 'First notification' in result.output
    assert '[OK] Session closed' in result.output


def test_default_mtu_is_flagged_as_assumed():
    replies = {'open': lambda d: FakeBox.ok(dict(OPENED, mtu=23, mtu_source='default'))}
    result, _ = run_session(['--listen', '0'], replies)
    assert 'MTU 23 (assumed' in result.output


def test_adapter_busy_is_actionable():
    replies = {'open': lambda d: FakeBox.err('adapter_busy', 'in use by an open BLE session')}
    result, _ = run_session(['--listen', '0'], replies)
    assert result.exit_code == 1
    assert 'in use by an open BLE session' in result.output
    assert 'lager ble sessions' in result.output
    assert '--force' in result.output


def test_bluez_missing_uses_the_remedy():
    replies = {'open': lambda d: FakeBox.err('bluez_unavailable', 'x')}
    result, _ = run_session(['--listen', '0'], replies)
    assert result.exit_code == 1
    assert net_helpers.BLUEZ_UNAVAILABLE_MESSAGE in result.output


def test_device_not_found_suggests_scan():
    replies = {'open': lambda d: FakeBox.err('device_not_found', 'Device not found')}
    result, _ = run_session(['--listen', '0'], replies)
    assert result.exit_code == 1
    assert 'lager ble scan' in result.output


def test_disconnect_mid_listen_prints_notifications_then_fails():
    replies = {'subscribe': lambda d: FakeBox.ok({'char': NOTIFY, 'handle': 10,
                                                  'mode': 'notify'}) + [
        FakeBox.notify((1, '01'), (2, '02')),
        ('ble_closed', {'reason': 'disconnected', 'message': 'The peripheral disconnected'})]}
    start = time.monotonic()
    result, fake = run_session(['--subscribe', NOTIFY, '--listen', '30'], replies)
    assert time.monotonic() - start < 5  # the close ended the listen early
    assert result.exit_code == 1
    assert '#2' in result.output
    assert 'The peripheral disconnected' in result.output
    assert result.output.index('#2') < result.output.index('peripheral disconnected')
    assert 'ble_close' not in [e for e, _ in fake.sent]


def test_prompt_exits_by_itself_when_the_session_ends():
    # Released by another client while the prompt waits for input: the pump
    # must wake the prompt instead of leaving a dead session at "ble>".
    import threading
    client, fake = make_client()
    client.open(ADDR)
    woken = threading.Event()

    def blocked_input(prompt):
        if woken.wait(3):
            raise KeyboardInterrupt
        return 'quit'

    threading.Timer(0.3, lambda: fake.handlers['ble_closed'](
        {'reason': 'released', 'message': 'Released by another client'})).start()
    start = time.monotonic()
    with patch('builtins.input', blocked_input), \
            patch.object(ble_cmd, '_wake_prompt', woken.set):
        ble_cmd._run_repl(client, ble_cmd._SessionPrinter(False), OPENED)
    assert woken.is_set()
    assert time.monotonic() - start < 2
    assert client.closed['reason'] == 'released'


def test_interactive_needs_a_terminal():
    result, _ = run_session([])
    assert result.exit_code == 1
    assert 'needs a terminal' in result.output


def test_bad_write_spec():
    result, _ = run_session(['--write', 'nothex'])
    assert result.exit_code == 2
    result, _ = run_session(['--write', f'{WRITE}:zz'])
    assert result.exit_code == 2


def test_force_releases_first():
    with patch.object(ble_cmd, '_release_ble_sessions',
                      return_value=[{'address': 'AA:BB:CC:DD:EE:09'}]) as release:
        result, fake = run_session(['--force', '--listen', '0'])
    assert result.exit_code == 0, result.output
    release.assert_called_once_with('192.0.2.4')
    assert 'Ended the session with AA:BB:CC:DD:EE:09' in result.output


def _http(status, body):
    resp = MagicMock(status_code=status)
    resp.json.return_value = body
    return resp


def run_sessions(args, method, resp):
    with patch.object(ble_cmd, 'resolve_box', return_value='192.0.2.4'), \
            patch.object(ble_cmd, 'resolve_box_locked', return_value='192.0.2.4'), \
            patch(f'requests.{method}', return_value=resp) as call, \
            patch('cli.gateway_auth.auth_headers_for_box', return_value={}), \
            patch('cli.box_storage._check_gateway', side_effect=lambda r, ip: r):
        result = CliRunner().invoke(ble_cmd.ble, ['sessions'] + args)
    return result, call


def test_sessions_lists_open_sessions():
    body = {'success': True, 'sessions': [
        {'address': ADDR, 'holder': 'tester', 'opened_s': 12.0, 'idle_s': 3.0,
         'mtu': 247, 'subscriptions': [NOTIFY]}]}
    result, call = run_sessions([], 'get', _http(200, body))
    assert result.exit_code == 0, result.output
    assert ADDR in result.output and 'tester' in result.output and NOTIFY in result.output
    assert call.call_args[0][0] == 'http://192.0.2.4:9000/ble/sessions'


def test_sessions_on_an_old_box():
    result, _ = run_sessions([], 'get', _http(404, {'error': 'The requested endpoint does not exist'}))
    assert result.exit_code == 1
    assert 'does not support BLE sessions' in result.output


def test_sessions_release():
    result, call = run_sessions(['--release', '--address', ADDR], 'post',
                                _http(200, {'success': True, 'released': [{'address': ADDR}]}))
    assert result.exit_code == 0, result.output
    assert call.call_args[1]['json'] == {'address': ADDR}
    assert f'Ended the session with {ADDR}' in result.output

    result, _ = run_sessions(['--release'], 'post',
                             _http(404, {'success': False, 'error': 'No open BLE session'}))
    assert result.exit_code == 0
    assert 'No BLE session was open' in result.output


# ---------------------------------------------------------------------------
# lager ble adapter / scan address types
# ---------------------------------------------------------------------------

def run_ble(args, value):
    resp = _http(200, {'success': True, 'value': value})
    with patch.object(ble_cmd, 'resolve_box', return_value='192.0.2.4'), \
            patch.object(ble_cmd, 'resolve_box_locked', return_value='192.0.2.4'), \
            patch('requests.post', return_value=resp) as post, \
            patch('cli.gateway_auth.auth_headers_for_box', return_value={}), \
            patch('cli.box_storage._check_gateway', side_effect=lambda r, ip: r):
        result = CliRunner().invoke(ble_cmd.ble, args)
    return result, post


def test_adapter_available():
    result, post = run_ble(['adapter'], {'available': True, 'reason': None, 'adapters': [
        {'name': 'hci0', 'address': 'C0:E3:50:76:0F:7D', 'powered': True}]})
    assert result.exit_code == 0, result.output
    assert 'hci0  C0:E3:50:76:0F:7D  powered' in result.output
    assert 'BLE is available' in result.output
    assert post.call_args[1]['json'] == {'action': 'adapter', 'params': {}}


def test_adapter_unavailable_exits_1_with_the_reason():
    result, _ = run_ble(['adapter'], {'available': False, 'adapters': [],
                                      'reason': 'The box has no Bluetooth adapter'})
    assert result.exit_code == 1
    assert 'BLE is not available' in result.output
    assert 'no Bluetooth adapter' in result.output


def test_adapter_json():
    value = {'available': True, 'reason': None, 'adapters': []}
    result, _ = run_ble(['adapter', '--json'], value)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == value


def test_scan_reports_address_types():
    devices = [
        {'name': 'peer', 'address': 'C0:E3:50:76:0F:7D', 'address_type': 'random',
         'random_type': 'static', 'rssi': -40, 'uuids': []},
        {'name': 'old box', 'address': 'AA:BB:CC:DD:EE:01', 'rssi': -50, 'uuids': []},
    ]
    result, _ = run_ble(['scan', '--verbose'], {'devices': devices})
    assert result.exit_code == 0, result.output
    assert 'C0:E3:50:76:0F:7D static' in ' '.join(result.output.split())
    listed = json.loads(result.output.split('JSON Output:')[1])
    assert (listed[0]['address_type'], listed[0]['random_type']) == ('random', 'static')
    assert (listed[1]['address_type'], listed[1]['random_type']) == (None, None)
