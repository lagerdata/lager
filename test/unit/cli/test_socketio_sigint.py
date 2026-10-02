# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Ctrl+C on a socket.io session belongs to the CLI, and exits promptly.

python-socketio and python-engineio install a process-wide SIGINT handler the
first time a client is built with the default `handle_sigint=True`. That
handler disconnects every client on the main thread *before*
KeyboardInterrupt is raised, so:

* the client's `disconnect` event clears `connected`, and the UART teardown's
  guard then skips `stop_uart`: the box was never told the session ended;
* the disconnect waits out websocket-client's fixed 3 s for a close frame the
  box's werkzeug server never sends, so every Ctrl+C took about 3 s.

All four CLI clients now pass `handle_sigint=False`, and the UART and RTT
teardowns bound that wait with `disconnect_bounded`.
"""

from __future__ import annotations

import importlib
import os
import signal
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

import engineio.base_client  # noqa: E402
import socketio.base_client  # noqa: E402

wsc = importlib.import_module("cli.commands.communication.websocket_client")

# (module, class, constructor args)
CLIENTS = [
    ("cli.commands.communication.websocket_client", "UARTWebSocketClient",
     ("http://box:9000", "UART", {})),
    ("cli.commands.development.debug.rtt_websocket_client", "RTTWebSocketClient",
     ("http://box:9000", "RTT")),
    ("cli.supply.websocket_client", "SupplyWebSocketClient",
     ("http://box:9000", "supply1")),
    ("cli.battery.websocket_client", "BatteryWebSocketClient",
     ("http://box:9000", "battery1")),
]


@pytest.fixture
def pristine_sigint(monkeypatch):
    """Undo any handler an earlier client in this process already installed.

    The libraries install it only once per process (guarded by a module
    global), so without this a test run after any default-constructed client
    would pass whatever the code under test did.
    """
    monkeypatch.setattr(socketio.base_client, "original_signal_handler", None)
    monkeypatch.setattr(engineio.base_client, "original_signal_handler", None)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    yield
    signal.signal(signal.SIGINT, previous)


@pytest.mark.parametrize("module, cls, args", CLIENTS, ids=[c[1] for c in CLIENTS])
def test_client_is_built_without_the_library_sigint_handler(module, cls, args, monkeypatch):
    mod = importlib.import_module(module)
    seen = {}

    class _Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def on(self, *a, **k):
            pass

    monkeypatch.setattr(mod.socketio, "Client", _Recorder)
    getattr(mod, cls)(*args)
    assert seen.get("handle_sigint") is False, seen


@pytest.mark.parametrize("module, cls, args", CLIENTS, ids=[c[1] for c in CLIENTS])
def test_building_a_client_leaves_sigint_alone(module, cls, args, pristine_sigint):
    getattr(importlib.import_module(module), cls)(*args)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


class _StuckSio:
    """A socket.io client whose disconnect waits on a close frame that never comes."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def disconnect(self):
        self.started.set()
        self.release.wait(10)


def test_disconnect_bounded_does_not_wait_for_a_silent_peer():
    sio = _StuckSio()
    t0 = time.monotonic()
    wsc.disconnect_bounded(sio, timeout=0.2)
    elapsed = time.monotonic() - t0
    sio.release.set()
    assert sio.started.is_set()
    assert elapsed < 1.0, elapsed


def test_disconnect_bounded_returns_as_soon_as_the_disconnect_does():
    class _Quick:
        calls = 0

        def disconnect(self):
            _Quick.calls += 1

    t0 = time.monotonic()
    wsc.disconnect_bounded(_Quick(), timeout=5.0)
    assert time.monotonic() - t0 < 1.0
    assert _Quick.calls == 1


@pytest.mark.parametrize("boom", [RuntimeError("nope"), KeyboardInterrupt()])
def test_disconnect_bounded_swallows_a_failing_disconnect(boom):
    class _Boom:
        def disconnect(self):
            raise boom

    wsc.disconnect_bounded(_Boom(), timeout=1.0)  # must not raise


class _CtrlCSio:
    """Connects, starts the session, then delivers Ctrl+C in the wait loop."""

    def __init__(self, client):
        self.client = client
        self.emitted = []
        self.disconnects = 0

    def connect(self, *a, **k):
        self.client.connected = True

    def emit(self, event, data=None, namespace=None):
        self.emitted.append(event)
        if event == "start_uart":
            self.client.uart_active = True

    def sleep(self, seconds):
        if self.client.uart_active and "stop_uart" not in self.emitted:
            raise KeyboardInterrupt

    def disconnect(self):
        self.disconnects += 1


def test_ctrl_c_in_a_uart_session_tells_the_box(monkeypatch):
    gateway = importlib.import_module("cli.gateway_auth")
    monkeypatch.setattr(gateway, "auth_headers_for_url", lambda url: {})
    client = wsc.UARTWebSocketClient("http://box:9000", "UART", {}, interactive=False)
    client._setup_terminal = lambda: None
    client._restore_terminal = lambda: None
    client.sio = _CtrlCSio(client)

    assert client.connect_and_run() == 0
    assert client.sio.emitted == ["start_uart", "stop_uart"]
    assert client.sio.disconnects == 1
