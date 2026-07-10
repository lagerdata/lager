# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the CLI's handling of box-side ``uart_status`` events.

When a UART device re-enumerates mid-session, the box now heals the session
in place and emits ``uart_status`` (reconnecting/reconnected) instead of
killing the stream. The CLI must show the notices WITHOUT stopping the
session — setting stop_event here would turn a transparent reconnect into a
user-visible disconnect. Boxes predating the event simply never send it.
"""

from __future__ import annotations

import importlib
import io
import os
import sys
import threading
import types
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

wsc = importlib.import_module('cli.commands.communication.websocket_client')


def _make_client():
    return wsc.UARTWebSocketClient('http://box:9000', 'uart1', {}, interactive=False)


def _fake_stderr():
    return types.SimpleNamespace(buffer=io.BytesIO())


def test_uart_status_handler_registered():
    client = _make_client()
    handlers = client.sio.handlers.get('/uart', {})
    assert 'uart_status' in handlers
    assert handlers['uart_status'] == client._on_uart_status


@pytest.mark.parametrize('payload,expected', [
    ({'status': 'reconnecting'}, b'reconnecting'),
    ({'status': 'reconnected', 'device_path': '/dev/ttyUSB1'}, b'/dev/ttyUSB1'),
])
def test_uart_status_prints_notice_without_stopping(payload, expected):
    client = _make_client()
    fake = _fake_stderr()
    with patch('sys.stderr', new=fake):
        client._on_uart_status(payload)
    assert expected in fake.buffer.getvalue()
    assert not client.stop_event.is_set()
    # The session must still be considered active for the run loop.
    assert isinstance(client.stop_event, threading.Event)


@pytest.mark.parametrize('payload', [None, {}, {'status': 'unknown-thing'}])
def test_uart_status_ignores_unknown_payloads(payload):
    client = _make_client()
    fake = _fake_stderr()
    with patch('sys.stderr', new=fake):
        client._on_uart_status(payload)
    assert fake.buffer.getvalue() == b''
    assert not client.stop_event.is_set()
