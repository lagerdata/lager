# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for how box/lager/python/service.py delimits its responses.

The box's JSON responses used to carry no Content-Length and no Connection
header. They worked anyway, for a reason nothing in box/ stated: nothing sets
``protocol_version``, so BaseHTTPRequestHandler defaults to HTTP/1.0,
``close_connection`` stays True, and the body is delimited by the socket
closing. That undeclared default is what let the CLI's ``resp.json()`` on a
``stream=True`` response terminate at all -- while ``parse_multipart``'s own
comment asserted the opposite ("this is a keep-alive connection").

So there are two things to pin: that JSON responses are now self-delimiting,
and that the HTTP/1.0 assumption the STREAMING path still rests on is not
changed by accident. Raising protocol_version to HTTP/1.1 without adding a
chunked encoder would leave every `lager python` run waiting for a body end
that never comes -- a hang, from a one-line change, with no other test to catch
it.
"""

import os
import sys
import types

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


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core', 'pigpio',
    'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
]:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.python.service import PythonServiceHandler  # noqa: E402


class RecordingWfile:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)
        return len(data)

    def flush(self):
        pass


def make_handler():
    """A handler wired up just enough for the real send_* methods to run."""
    handler = PythonServiceHandler.__new__(PythonServiceHandler)
    handler.wfile = RecordingWfile()
    handler.request_version = 'HTTP/1.1'      # what the CLI sends
    handler.requestline = 'POST /python HTTP/1.1'
    handler.client_address = ('10.0.0.9', 51000)
    handler.command = 'POST'
    handler.path = '/python'
    handler._headers_buffer = []
    handler.log_request = lambda *a, **kw: None
    return handler


def split_response(raw):
    """(status line, {header: value}, body) from raw response bytes."""
    head, _, body = raw.partition(b'\r\n\r\n')
    lines = head.decode('latin-1').split('\r\n')
    headers = {}
    for line in lines[1:]:
        if ':' in line:
            name, _, value = line.partition(':')
            headers[name.strip().lower()] = value.strip()
    return lines[0], headers, body


class TestJsonResponses:

    def test_body_is_self_delimiting(self):
        handler = make_handler()
        handler.send_json_response(200, {'status': 'detached'})

        _status, headers, body = split_response(bytes(handler.wfile.buf))
        assert 'content-length' in headers
        assert int(headers['content-length']) == len(body)

    def test_length_counts_encoded_bytes_not_characters(self):
        """A non-ASCII payload is where len(str) and len(bytes) diverge.

        An over-long Content-Length is not a cosmetic bug: the client waits for
        bytes that never arrive. Pip errors, filenames and lock holders can all
        carry non-ASCII.
        """
        handler = make_handler()
        handler.send_json_response(422, {'error': 'café — naïve'})

        _status, headers, body = split_response(bytes(handler.wfile.buf))
        assert int(headers['content-length']) == len(body)
        assert len(body) > len('{"error": "cafe -- naive"}')

    def test_declares_connection_close(self):
        handler = make_handler()
        handler.send_json_response(200, {'ok': True})

        _status, headers, _body = split_response(bytes(handler.wfile.buf))
        assert headers.get('connection') == 'close'

    def test_error_responses_inherit_the_framing(self):
        handler = make_handler()
        handler.send_error_response(500, 'boom')

        _status, headers, body = split_response(bytes(handler.wfile.buf))
        assert int(headers['content-length']) == len(body)
        assert b'boom' in body


class TestStreamingResponses:

    def test_declares_close_and_carries_no_length(self):
        """A Content-Length here would be a lie -- the length is unknowable.

        So this body stays close-delimited, and now says so rather than relying
        on the HTTP/1.0 default to mean it.
        """
        handler = make_handler()
        handler.send_streaming_response(iter([b'1 2 hi']))

        _status, headers, body = split_response(bytes(handler.wfile.buf))
        assert headers.get('connection') == 'close'
        assert 'content-length' not in headers
        assert headers.get('lager-output-version') == '1'
        assert body == b'1 2 hi'


class TestTheAssumptionStreamingRestsOn:

    def test_protocol_version_is_http_1_0(self):
        """Pinned deliberately.

        Raising this to HTTP/1.1 keeps the connection alive, and the streaming
        path has neither a length nor a chunked encoder -- so every streamed
        run would hang waiting for an end that never comes. If someone wants
        1.1, they have to add chunked encoding and change this test on purpose.
        """
        assert PythonServiceHandler.protocol_version == 'HTTP/1.0'
