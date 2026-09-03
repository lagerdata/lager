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
