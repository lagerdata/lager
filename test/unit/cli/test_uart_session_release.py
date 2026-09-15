# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the CLI's UART teardown and its "net in use" reporting.

Releasing the box-side session used to live on the normal exit path only, so a
Ctrl+C skipped it and left the net's release to the socket.io disconnect alone.
When that disconnect did not land either -- a second Ctrl+C during teardown, a
container torn down without a FIN -- the box kept the net, and every retry hit
"already in use by another session".

So: stop_uart goes out on every exit path, it goes out whenever start_uart did
(not merely when the session came up), and nothing short of process death may
skip the disconnect that follows it.
"""

from __future__ import annotations

import importlib
import io
import os
import sys
import types
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

wsc = importlib.import_module('cli.commands.communication.websocket_client')
uart_cmd = importlib.import_module('cli.commands.communication.uart')


class FakeSio:
    """Stands in for the socketio.Client, recording the teardown handshake."""

    def __init__(self, emit_raises=None, disconnect_raises=None):
        self.emitted = []
        self.disconnects = 0
        self.slept = []
        self._emit_raises = emit_raises
        self._disconnect_raises = disconnect_raises

    def emit(self, event, data=None, namespace=None):
        if self._emit_raises is not None:
            raise self._emit_raises
        self.emitted.append((event, namespace))

    def sleep(self, seconds):
        self.slept.append(seconds)

    def disconnect(self):
        self.disconnects += 1
        if self._disconnect_raises is not None:
            raise self._disconnect_raises


def _make_client(box_label=None, **kwargs):
    client = wsc.UARTWebSocketClient(
        'http://box:9000', 'UART', {}, interactive=False, box_label=box_label)
    client.sio = FakeSio(**kwargs)
    return client


def _fake_stderr():
    return types.SimpleNamespace(buffer=io.BytesIO())


# ---------- teardown ----------

def test_release_emits_stop_uart_then_disconnects():
    client = _make_client()
    client.connected = True
    client.start_emitted = True

    client._release_box_session()

    assert client.sio.emitted == [('stop_uart', '/uart')]
    assert client.sio.disconnects == 1


def test_release_emits_stop_uart_even_when_session_never_came_up():
    # The orphan-from-birth case: we gave up waiting for uart_connected, but
    # the box may have registered the session just after our deadline, so it
    # still needs telling.
    client = _make_client()
    client.connected = True
    client.start_emitted = True
    client.uart_active = False

    client._release_box_session()

    assert ('stop_uart', '/uart') in client.sio.emitted
    assert client.sio.disconnects == 1


def test_release_skips_stop_uart_when_start_never_went_out():
    client = _make_client()
    client.connected = True
    client.start_emitted = False

    client._release_box_session()

    assert client.sio.emitted == []
    assert client.sio.disconnects == 1


def test_release_does_nothing_when_never_connected():
    client = _make_client()
    client.connected = False
    client.start_emitted = True

    client._release_box_session()

    assert client.sio.emitted == []
    assert client.sio.disconnects == 0


@pytest.mark.parametrize('boom', [KeyboardInterrupt(), RuntimeError('nope')])
def test_disconnect_still_runs_when_stop_uart_blows_up(boom):
    # A second Ctrl+C landing on the stop_uart emit must not cost us the
    # disconnect -- on an older box that disconnect is the only thing that
    # frees the net.
    client = _make_client(emit_raises=boom)
    client.connected = True
    client.start_emitted = True

    client._release_box_session()

    assert client.sio.disconnects == 1


@pytest.mark.parametrize('boom', [KeyboardInterrupt(), RuntimeError('nope')])
def test_release_swallows_a_failing_disconnect(boom):
    # Teardown runs in a finally; it must never replace the real exit reason.
    client = _make_client(disconnect_raises=boom)
    client.connected = True
    client.start_emitted = True

    client._release_box_session()  # must not raise

    assert client.sio.disconnects == 1


# ---------- "net in use" reporting ----------

def test_in_use_error_names_the_take_over_command():
    client = _make_client(box_label='test-box')
    fake = _fake_stderr()
    with patch('sys.stderr', new=fake):
        client._on_error({
            'message': "UART net 'UART' is already in use by another session",
            'code': 'net_in_use',
            'netname': 'UART',
        })
    out = fake.buffer.getvalue().decode()
    assert 'lager uart UART --force --box test-box' in out
    assert client.stop_event.is_set()


def test_in_use_hint_omits_box_when_unknown():
    client = _make_client(box_label=None)
    fake = _fake_stderr()
    with patch('sys.stderr', new=fake):
        client._on_error({'message': 'held', 'code': 'net_in_use'})
    out = fake.buffer.getvalue().decode()
    assert 'lager uart UART --force' in out
    assert '--box' not in out


def test_error_without_code_prints_only_the_message():
    # A box too old to send 'code' must not gain a bogus hint.
    client = _make_client(box_label='test-box')
    fake = _fake_stderr()
    with patch('sys.stderr', new=fake):
        client._on_error({'message': 'something else went wrong'})
    out = fake.buffer.getvalue().decode()
    assert 'something else went wrong' in out
    assert '--force' not in out


# ---------- the banner that mangled a device path ----------

def test_banner_identity_keeps_a_by_id_path_readable():
    # This exact path used to render as "/dev/seria", which reads as a
    # corrupted net record rather than a truncation.
    path = '/dev/serial/by-id/usb-Prolific_USB-Serial_0001-if00'
    assert uart_cmd._shorten_identity(path) == path


def test_banner_identity_middle_ellipsises_when_too_long():
    path = '/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_0001-if00'
    shortened = uart_cmd._shorten_identity(path)
    assert len(shortened) <= 56
    assert shortened.startswith('/dev/serial')
    # The distinguishing tail is what tells two adapters apart; keep it.
    assert shortened.endswith('0001-if00')
    assert '...' in shortened


@pytest.mark.parametrize('value', ['0001', '/dev/ttyACM1', 'unknown', ''])
def test_banner_identity_leaves_short_values_alone(value):
    assert uart_cmd._shorten_identity(value) == value


def test_banner_identity_tolerates_a_missing_pin():
    # net_config.get("pin") can be absent; must not raise on a non-str.
    assert uart_cmd._shorten_identity(None) is None


# ---------- --force against a box that cannot release ----------

class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


def _release_with(monkeypatch, resp):
    import cli.box_storage as box_storage
    import cli.gateway_auth as gateway_auth
    monkeypatch.setattr(uart_cmd.requests, 'delete', lambda *a, **k: resp)
    monkeypatch.setattr(gateway_auth, 'auth_headers_for_box', lambda ip: {})
    monkeypatch.setattr(box_storage, '_check_gateway', lambda r, ip, **k: r)
    return uart_cmd._release_uart_session(None, '1.2.3.4', 'UART')


def test_force_reports_a_released_session(monkeypatch, capsys):
    resp = _Resp(200, {'released': ['sid'], 'netname': 'UART'})
    assert _release_with(monkeypatch, resp) is True
    assert capsys.readouterr().err == ''


def test_force_on_a_current_box_with_nothing_held_is_silent(monkeypatch, capsys):
    resp = _Resp(404, {'error': "No UART session is holding net 'UART'", 'released': []})
    assert _release_with(monkeypatch, resp) is False
    assert capsys.readouterr().err == ''


def test_force_on_a_box_without_the_release_route_warns(monkeypatch, capsys):
    # A box older than the route answers 404 with no such body. Saying nothing
    # left the user facing the same in-use error with no hint why.
    assert _release_with(monkeypatch, _Resp(404)) is False
    assert 'too old to support --force' in capsys.readouterr().err


def test_force_on_a_box_that_rejects_the_method_warns(monkeypatch, capsys):
    assert _release_with(monkeypatch, _Resp(405)) is False
    assert 'too old to support --force' in capsys.readouterr().err


def test_in_use_hint_names_the_net_that_holds_the_device():
    # Two nets on one device: releasing the requested net frees nothing, so
    # the take-over command must name the holder the box reports.
    client = _make_client(box_label='test-box')
    fake = _fake_stderr()
    with patch('sys.stderr', new=fake):
        client._on_error({
            'message': "UART device /dev/ttyUSB0 is already in use by net 'SERIAL1'",
            'code': 'net_in_use',
            'netname': 'SERIAL2',
            'held_by': 'SERIAL1',
        })
    out = fake.buffer.getvalue().decode()
    assert 'lager uart SERIAL1 --force --box test-box' in out


# ---------- --sessions is read-only, so it takes no box lock ----------

def test_sessions_does_not_take_the_box_lock(monkeypatch):
    # Under the box lock, a second user could not even list who held the net
    # they had just been refused.
    import cli.box_storage as box_storage
    from click.testing import CliRunner

    calls = {}

    def resolve_without_lock(ctx, box, **kwargs):
        calls['kwargs'] = kwargs
        return '1.2.3.4', 'b'

    def locking_resolver(*_args, **_kwargs):
        raise AssertionError('--sessions must not take the box lock')

    monkeypatch.setattr(box_storage, 'resolve_and_validate_box_with_name',
                        resolve_without_lock)
    monkeypatch.setattr(uart_cmd, '_resolve_box_with_name', locking_resolver)
    monkeypatch.setattr(uart_cmd, 'resolve_box_locked', locking_resolver)
    monkeypatch.setattr(uart_cmd, 'display_uart_sessions',
                        lambda ctx, ip: calls.setdefault('listed', ip))

    result = CliRunner().invoke(uart_cmd.uart, ['--sessions', '--box', 'b'])

    assert result.exit_code == 0, result.output
    assert calls['kwargs'] == {'_skip_lock_check': True}
    assert calls['listed'] == '1.2.3.4'
