# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the oscilloscope daemon client
(box/lager/measurement/scope/daemon_client.py).

One WebSocket carries replies, pushed captures and pushed state, so most of
what can go wrong is reading the wrong frame as the answer, or a connection
failure escaping as something no caller catches. The socket here is scripted.
"""

import json
import os
import sys
import time
import types
from itertools import islice
from unittest.mock import MagicMock

import pytest


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


for _dep in ('pyvisa', 'pyvisa.constants', 'usb', 'usb.util', 'usb.core',
             'serial', 'serial.tools', 'serial.tools.list_ports'):
    if _dep not in sys.modules:
        sys.modules[_dep] = _make_module(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.measurement.scope import daemon_client, lscp  # noqa: E402


def _reply(response, **fields):
    return json.dumps({"Response": dict(fields, response=response)})


class _Socket:
    """A WebSocket that answers from a script: text, bytes, or an exception."""

    def __init__(self, *script):
        self.script = list(script)
        self.sent = []
        self.closed = False

    def send(self, text):
        self.sent.append(json.loads(text))

    def receive(self, timeout=None):
        if not self.script:
            time.sleep(min(timeout or 0, 0.01))
            return None
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


@pytest.fixture
def sockets(monkeypatch):
    """Sockets handed out in order, one per connection the client opens."""
    queue = []
    fake = types.SimpleNamespace(Client=lambda url, thread_class=None: queue.pop(0))
    monkeypatch.setitem(sys.modules, "simple_websocket", fake)
    # Frames here are opaque bytes; decoding is lscp's to test.
    monkeypatch.setattr(lscp, "decode", lambda frame: frame)
    return queue


class TestCommands:

    def test_a_connection_lost_mid_capture_is_reported_as_unavailable(self, sockets):
        # The capture frame was read outside the handler that turns a dropped
        # connection into ScopeDaemonUnavailable, so the raw library error
        # escaped every caller that catches ScopeDaemonError.
        sockets.append(_Socket(_reply("TriggeredData", seq=5), ConnectionError("closed")))
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        with pytest.raises(daemon_client.ScopeDaemonUnavailable):
            client.command("GetTriggeredData")
        assert client._ws is None

    def test_pushed_state_is_not_taken_for_a_reply(self, sockets):
        sockets.append(_Socket(_reply("State", state={"acquiring": True}),
                               _reply("SampleRate", sample_rate=1e8)))
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        assert client.command("GetSampleRate")["sample_rate"] == 1e8

    def test_get_state_is_answered_with_a_state(self, sockets):
        sockets.append(_Socket(_reply("State", state={"acquiring": True})))
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        assert client.command("GetState")["state"] == {"acquiring": True}

    def test_a_reply_that_never_comes_is_a_timeout(self, sockets):
        sockets.append(_Socket())
        client = daemon_client.ScopeDaemonClient(timeout=0.05)
        with pytest.raises(daemon_client.ScopeDaemonTimeout):
            client.command("GetSampleRate")

    def test_an_error_reply_leaves_the_connection_usable(self, sockets):
        socket = _Socket(_reply("Error", message="no capture yet"),
                         _reply("SampleRate", sample_rate=1e8))
        sockets.append(socket)
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        with pytest.raises(daemon_client.ScopeDaemonError, match="no capture yet"):
            client.command("GetTriggeredData")
        assert client.command("GetSampleRate")["sample_rate"] == 1e8
        assert not socket.closed


class TestCaptures:

    def _subscribed(self, *frames):
        return _Socket(_reply("Subscribed"), *frames)

    def test_each_capture_is_yielded_once_in_order(self, sockets):
        sockets.append(self._subscribed(b"one", b"two", b"three"))
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        assert list(islice(client.captures(), 3)) == [b"one", b"two", b"three"]

    def test_it_subscribes_with_credit_and_returns_one_per_capture(self, sockets):
        # Credit is what bounds memory: without it every capture is pushed
        # whether or not the reader keeps up, and they queue in the client.
        stream = self._subscribed(b"one", b"two", b"three")
        sockets.append(stream)
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        list(islice(client.captures(), 3))
        assert stream.sent[0] == {"command": "Subscribe",
                                  "credits": daemon_client.STREAM_CREDITS}
        assert stream.sent[1:] == [{"command": "Credit", "count": 1}] * 2

    def test_it_streams_on_a_connection_of_its_own(self, sockets):
        control = _Socket(_reply("SampleRate", sample_rate=1e8))
        stream = self._subscribed(b"one")
        sockets.extend([control, stream])
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        client.command("GetSampleRate")
        assert list(islice(client.captures(), 1)) == [b"one"]
        assert all(sent["command"] != "Subscribe" for sent in control.sent)

    def test_stopping_early_closes_the_stream_connection(self, sockets):
        stream = self._subscribed(b"one", b"two")
        sockets.append(stream)
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        captures = client.captures()
        next(captures)
        captures.close()
        assert stream.closed

    def test_no_capture_in_time_is_a_timeout_that_says_why(self, sockets):
        sockets.append(self._subscribed())
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        with pytest.raises(daemon_client.ScopeDaemonTimeout, match="start the scope"):
            next(client.captures(timeout=0.05))

    def test_a_deadline_ends_the_stream_quietly(self, sockets):
        sockets.append(self._subscribed(b"one"))
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        started = time.monotonic()
        frames = list(client.captures(timeout=5.0, until=started + 0.1))
        assert frames == [b"one"]
        assert time.monotonic() - started < 2.0

    def test_a_connection_lost_mid_stream_is_reported_as_unavailable(self, sockets):
        sockets.append(self._subscribed(b"one", ConnectionError("closed")))
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        captures = client.captures()
        next(captures)
        with pytest.raises(daemon_client.ScopeDaemonUnavailable):
            next(captures)

    def test_an_error_notice_on_the_stream_is_raised(self, sockets):
        sockets.append(self._subscribed(_reply("Error", message="unit unplugged")))
        client = daemon_client.ScopeDaemonClient(timeout=1.0)
        with pytest.raises(daemon_client.ScopeDaemonError, match="unit unplugged"):
            next(client.captures())
