#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for `lager debug <net> gdbserver --rtt --interactive`.

Pins the contract of the bi-directional RTT mode:

  * `--interactive` without `--rtt`/`--rtt-reset` is rejected up front,
    before any box traffic.
  * With `--rtt --interactive`, the command still connects the gdbserver
    through the debug service (:8765) but then hands the streaming leg to
    the /rtt WebSocket client (:9000) instead of the read-only HTTP chunked
    stream — passing the saved net's name, the requested channel, and any
    RTT search overrides.

The box is mocked at the `DebugServiceClient` boundary and the WebSocket
client at `connect_rtt_interactive`, so no hardware or network is touched.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

# python-socketio is a runtime dep of the CLI but not of the unit suite
# (test/requirements-unit.txt mocks-not-installs policy). The websocket client
# module imports it at module level; a MagicMock-backed stub is enough since
# these tests patch connect_rtt_interactive and never open a real socket.
try:
    import socketio  # noqa: F401
except ImportError:
    _stub = types.ModuleType("socketio")
    _stub.__getattr__ = lambda attr: MagicMock()
    sys.modules["socketio"] = _stub

debug_mod = importlib.import_module("cli.commands.development.debug.commands")
rtt_ws_mod = importlib.import_module(
    "cli.commands.development.debug.rtt_websocket_client")

BOX_IP = "1.2.3.4"

NET = {
    "name": "dbg1",
    "role": "debug",
    "channel": "NRF52840_XXAA",
    "address": "USB0::0x1366::0x0101::000051014439::INSTR",
}


class _Obj:
    """Settable stand-in for the LagerContext (the group stashes `net_name`)."""


class FakeClient:
    """DebugServiceClient stand-in recording every box call in order."""

    def __init__(self):
        self.calls = []

    def get_info(self, net):
        self.calls.append(("get_info", net))
        return {"connected": False}

    def connect(self, net, speed=None, force=False, halt=False, gdb=True,
                gdb_port=None, jlink_script=None, openocd_config=None):
        self.calls.append(("connect", net))
        return {"gdb_server": {"status": "started", "gdb_port": 2331},
                "backend": "jlink"}

    def disconnect(self, net, keep_jlink_running=False):
        self.calls.append(("disconnect", net))
        return {}

    def reset(self, net, halt=False):
        self.calls.append(("reset", net))
        return {}

    def rtt(self, net=None, channel=0, timeout=None, **kwargs):
        self.calls.append(("rtt", net))
        return iter(())

    def close(self):
        self.calls.append(("close", None))


def run_gdbserver(client, args, ws_exit_code=0):
    """Invoke gdbserver with everything below the CLI mocked.

    Returns (result, ws_calls) where ws_calls records every
    connect_rtt_interactive invocation as (box_url, netname, kwargs).
    """
    obj = _Obj()
    obj.net_name = NET["name"]
    ws_calls = []

    def fake_connect_rtt(box_url, netname, channel=0, search_params=None):
        ws_calls.append((box_url, netname,
                         {"channel": channel, "search_params": search_params}))
        return ws_exit_code

    with patch.object(debug_mod, "_resolve_box_with_username",
                      lambda ctx, box: (BOX_IP, "lagerdata")), \
         patch.object(debug_mod, "_get_debug_net",
                      lambda ctx, box, net_name=None: NET), \
         patch.object(debug_mod, "_resolve_debug_scripts",
                      lambda ctx, name, debug_net: (None, None)), \
         patch.object(debug_mod, "_get_service_client", lambda box: client), \
         patch.object(rtt_ws_mod, "connect_rtt_interactive",
                      fake_connect_rtt), \
         patch("time.sleep", lambda *a, **k: None):
        result = CliRunner().invoke(debug_mod.gdbserver, args, obj=obj,
                                    catch_exceptions=False)
    return result, ws_calls


def test_interactive_requires_rtt():
    result, ws_calls = run_gdbserver(FakeClient(), ["--interactive"])
    assert result.exit_code == 1
    assert "--interactive requires --rtt or --rtt-reset" in result.output
    assert ws_calls == []


def test_interactive_rejected_before_any_box_traffic():
    client = FakeClient()
    run_gdbserver(client, ["--interactive"])
    assert client.calls == []


def test_interactive_routes_to_websocket_client():
    client = FakeClient()
    result, ws_calls = run_gdbserver(client, ["--rtt", "--interactive"])

    assert result.exit_code == 0
    # gdbserver still started through the debug service...
    assert ("connect", NET) in client.calls
    # ...but the streaming leg went to the /rtt WebSocket, not HTTP.
    assert all(name != "rtt" for name, _ in client.calls)
    assert ws_calls == [
        (f"http://{BOX_IP}:9000", "dbg1",
         {"channel": 0, "search_params": {}}),
    ]


def test_interactive_forwards_channel_and_search_params():
    client = FakeClient()
    result, ws_calls = run_gdbserver(
        client,
        ["--rtt", "--interactive", "--rtt-channel", "1",
         "--rtt-search-addr", "0x20020000", "--rtt-search-size", "0x4000"])

    assert result.exit_code == 0
    assert ws_calls == [
        (f"http://{BOX_IP}:9000", "dbg1",
         {"channel": 1,
          "search_params": {"search_addr": 0x20020000,
                            "search_size": 0x4000}}),
    ]


def test_interactive_propagates_websocket_exit_code():
    result, ws_calls = run_gdbserver(
        FakeClient(), ["--rtt", "--interactive"], ws_exit_code=3)
    assert result.exit_code == 3
    assert len(ws_calls) == 1


def test_non_interactive_rtt_keeps_http_stream():
    client = FakeClient()
    result, ws_calls = run_gdbserver(client, ["--rtt"])

    assert result.exit_code == 0
    assert ws_calls == []
    assert ("rtt", NET) in client.calls
