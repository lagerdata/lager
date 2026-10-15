# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for interactive UART echo handling.

After each line typed in `lager uart -i`, the client used to discard the first
received line unconditionally, assuming it was the device echoing the command.
A device that does not echo lost its real reply, and a reply with no line
ending was hidden until some later line ending arrived.

The client now drops that line only when it actually is the command (CR/LF,
ANSI escapes and a leading prompt ignored), and --no-echo turns the filter off.
"""

from __future__ import annotations

import importlib
import io
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

wsc = importlib.import_module('cli.commands.communication.websocket_client')
uart_cmd = importlib.import_module('cli.commands.communication.uart')


class FakeSio:
    def __init__(self):
        self.written = []

    def emit(self, event, data=None, namespace=None):
        if event == 'uart_write':
            self.written.append(bytes.fromhex(data['data']))


def _make_client(expect_echo=True):
    client = wsc.UARTWebSocketClient('http://box:9000', 'uart1', {'line_ending': 'cr'},
                                     interactive=True, expect_echo=expect_echo)
    client.sio = FakeSio()
    return client


def _fake_stdout():
    return types.SimpleNamespace(buffer=io.BytesIO())


def _receive(client, *chunks):
    """Feed chunks through _on_uart_data; return everything written to stdout."""
    fake = _fake_stdout()
    with patch('sys.stdout', new=fake):
        for chunk in chunks:
            client._on_uart_data({'data': chunk.hex()})
    return fake.buffer.getvalue()


def _sent(client, cmd):
    """Arm the filter exactly as pressing Enter after `cmd` does."""
    with patch('sys.stdout', new=_fake_stdout()):
        client._arm_echo_filter(cmd)


def _type(client, keys):
    """Drive the real stdin loop with `keys`; return what went to stdout."""
    fake = _fake_stdout()
    reads = [keys, b'']
    client.uart_active = True
    with patch('sys.stdout', new=fake), \
            patch('sys.stdin', new=types.SimpleNamespace(fileno=lambda: 0)), \
            patch.object(wsc.select, 'select', lambda r, w, x, t: (r, [], [])), \
            patch.object(wsc.os, 'read', lambda fd, n: reads.pop(0)):
        client._read_stdin_thread()
    return fake.buffer.getvalue()


# ---------- devices that echo: unchanged ----------

def test_echoing_device_has_its_echo_dropped():
    client = _make_client()
    _sent(client, b'help')
    out = _receive(client, b'help\r\ncommands: a b c\r\ncompat> ')
    # Same bytes as before the fix: echo and its CR dropped, the rest shown.
    assert out == b'\ncommands: a b c\r\ncompat> '
    assert not client.suppress_next_line


def test_echo_split_mid_command_is_still_dropped():
    client = _make_client()
    _sent(client, b'status')
    out = _receive(client, b'sta', b'tus', b'\r\nOK\r\n')
    assert out == b'\nOK\r\n'


def test_echo_in_its_own_chunk_then_reply():
    client = _make_client()
    _sent(client, b'$?')
    assert _receive(client, b'$?\r') == b''
    assert _receive(client, b'\n#HOME$\r\n') == b'\n#HOME$\r\n'


def test_echo_wrapped_in_ansi_and_behind_a_prompt_is_dropped():
    client = _make_client()
    _sent(client, b'ver')
    out = _receive(client, b'\x1b[32mcompat> \x1b[0mver\r\nv1.2\r\n')
    assert out == b'\nv1.2\r\n'


def test_leftover_line_ending_before_the_echo_is_passed_through():
    client = _make_client()
    _sent(client, b'ver')
    assert _receive(client, b'\nver\r\nv1.2\r\n') == b'\n\nv1.2\r\n'


# ---------- devices that do not echo ----------

def test_non_echoing_reply_is_printed_in_full():
    # The original report: `$?` answered by `#HOME$\r\n`, shown as a blank line.
    client = _make_client()
    _sent(client, b'$?')
    assert _receive(client, b'#HOME$\r\n') == b'#HOME$\r\n'
    assert not client.suppress_next_line


def test_non_echoing_reply_split_across_chunks():
    client = _make_client()
    _sent(client, b'$?')
    assert _receive(client, b'#HO', b'ME$\r', b'\n') == b'#HOME$\r\n'


def test_reply_without_line_ending_is_shown_at_once():
    client = _make_client()
    _sent(client, b'$?')
    assert _receive(client, b'OK') == b'OK'
    assert not client.suppress_next_line
    # Later output is untouched too.
    assert _receive(client, b'>') == b'>'


def test_reply_that_merely_ends_with_the_command_is_kept():
    client = _make_client()
    _sent(client, b'$?')
    assert _receive(client, b'ERR $?\r\n') == b'ERR $?\r\n'


def test_held_prefix_is_flushed_when_the_next_command_is_sent():
    # A reply that looks like the start of the command is held; it must not
    # be lost if the device never finishes the line.
    client = _make_client()
    _sent(client, b'OK?')
    assert _receive(client, b'O') == b''
    fake = _fake_stdout()
    with patch('sys.stdout', new=fake):
        client._arm_echo_filter(b'next')
    assert fake.buffer.getvalue() == b'O'
    assert client.pending_echo == b'next'


# ---------- what pressing Enter arms ----------

def test_enter_arms_the_filter_with_the_typed_command():
    client = _make_client()
    _type(client, b'$?\r')
    assert client.sio.written == [b'$?\r']
    assert client.suppress_next_line
    assert client.pending_echo == b'$?'


def test_empty_line_does_not_arm_the_filter():
    client = _make_client()
    _type(client, b'\r')
    assert client.sio.written == [b'\r']
    assert not client.suppress_next_line
    assert _receive(client, b'prompt>\r\n') == b'prompt>\r\n'


def test_no_echo_never_arms_the_filter():
    client = _make_client(expect_echo=False)
    _type(client, b'$?\r')
    assert client.sio.written == [b'$?\r']
    assert not client.suppress_next_line
    # Even a line identical to the command is real output under --no-echo.
    assert _receive(client, b'$?\r\n#HOME$\r\n') == b'$?\r\n#HOME$\r\n'


# ---------- --echo/--no-echo wiring ----------

def _live_wsc():
    # _connect_uart_http imports the client lazily, from sys.modules. Other
    # tests in this suite re-import that module, so patch whichever copy is
    # live now rather than the one bound when this file was collected.
    return importlib.import_module('cli.commands.communication.websocket_client')


def test_help_lists_the_no_echo_flag():
    from click.testing import CliRunner
    result = CliRunner().invoke(uart_cmd.uart, ['--help'])
    assert result.exit_code == 0
    assert '--echo / --no-echo' in result.output


@pytest.mark.parametrize('expect_echo', [True, False])
def test_interactive_connect_forwards_expect_echo(monkeypatch, expect_echo):
    seen = {}

    def fake_connect(box_url, netname, overrides, box_label=None, expect_echo=True):
        seen['expect_echo'] = expect_echo
        seen['overrides'] = dict(overrides)
        return 0

    monkeypatch.setattr(_live_wsc(), 'connect_uart_interactive', fake_connect)
    uart_cmd._connect_uart_http(MagicMock(), '1.2.3.4', 'uart1', {'baudrate': 115200},
                                True, expect_echo=expect_echo)
    assert seen['expect_echo'] is expect_echo
    # Client-side only: the box never sees it.
    assert 'expect_echo' not in seen['overrides']


def test_read_only_connect_takes_no_echo_argument(monkeypatch):
    seen = {}

    def fake_readonly(box_url, netname, overrides, box_label=None):
        seen['called'] = True
        return 0

    monkeypatch.setattr(_live_wsc(), 'connect_uart_readonly', fake_readonly)
    uart_cmd._connect_uart_http(MagicMock(), '1.2.3.4', 'uart1', {}, False,
                                expect_echo=False)
    assert seen['called']
