#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""`lager debug <net> gdbserver` on a box behind a gateway, and on a plain box.

A gated box does not publish its GDB port, so the command must never print
`target remote <box>:<port>` there: it opens a local tunnel, prints
`localhost:<port>`, and stays in the foreground until Ctrl-C. On a plain box
nothing changes -- the command returns and prints the box's own address.

The route decision (`choose_route`) and the tunnel (`GatewayTunnel`) are
replaced at the command's boundary; ``test_gateway_tunnel.py`` covers both.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

try:
    import socketio  # noqa: F401
except ImportError:
    _stub = types.ModuleType("socketio")
    _stub.__getattr__ = lambda attr: MagicMock()
    sys.modules["socketio"] = _stub

debug_mod = importlib.import_module("cli.commands.development.debug.commands")
rtt_ws_mod = importlib.import_module(
    "cli.commands.development.debug.rtt_websocket_client")
from cli.errors import LagerError  # noqa: E402

BOX_IP = "10.9.8.7"
NET = {"name": "dbg1", "role": "debug", "channel": "NRF52840_XXAA",
       "address": "USB0::0x1366::0x0101::000051014439::INSTR"}


class _Obj:
    pass


class FakeClient:
    def __init__(self, status="started", port=2331):
        self.calls = []
        self.status = status
        self.port = port

    def get_info(self, net):
        return {"connected": False}

    def connect(self, net, **kwargs):
        self.calls.append("connect")
        return {"gdb_server": {"status": self.status, "gdb_port": self.port},
                "backend": "jlink"}

    def disconnect(self, net, keep_jlink_running=False):
        self.calls.append(("disconnect", keep_jlink_running))
        return {"status": "disconnected", "backend": "jlink", "gdb_port": self.port}

    def reset(self, net, halt=False):
        self.calls.append("reset")

    def rtt(self, net=None, channel=0, timeout=None, **kwargs):
        self.calls.append("rtt")
        return iter([b"hello"])

    def close(self):
        self.calls.append("close")


class FakeTunnel:
    """GatewayTunnel stand-in. serve_forever behaves like Ctrl-C by default."""

    instances = []

    def __init__(self, box_ip, remote_port, *, local_port=None, box_label=None, **_):
        self.box_ip = box_ip
        self.remote_port = remote_port
        self.local_port = remote_port if local_port is None else local_port
        self.events = []
        self.serve_raises = KeyboardInterrupt
        FakeTunnel.instances.append(self)

    def bind(self):
        self.events.append("bind")
        return self.local_port

    def serve_forever(self):
        self.events.append("serve_forever")
        raise self.serve_raises()

    def start(self):
        self.events.append("start")

    def close(self):
        self.events.append("close")


def run(args, *, route=debug_mod.ROUTE_DIRECT, client=None, tunnel_cls=FakeTunnel,
        box="STG-1"):
    FakeTunnel.instances = []
    client = client or FakeClient()
    obj = _Obj()
    obj.net_name = NET["name"]
    routes = []

    def fake_route(box_ip, port):
        routes.append((box_ip, port))
        if isinstance(route, Exception):
            raise route
        return route

    argv = list(args) + (["--box", box] if box else [])
    with patch.object(debug_mod, "_resolve_box_with_username",
                      lambda ctx, b: (BOX_IP, "lagerdata")), \
         patch.object(debug_mod, "_get_debug_net",
                      lambda ctx, b, net_name=None: NET), \
         patch.object(debug_mod, "_resolve_debug_scripts",
                      lambda ctx, name, net: (None, None)), \
         patch.object(debug_mod, "_get_service_client", lambda b: client), \
         patch.object(debug_mod, "choose_route", fake_route), \
         patch.object(debug_mod, "GatewayTunnel", tunnel_cls), \
         patch.object(rtt_ws_mod, "connect_rtt_interactive",
                      lambda *a, **k: 0), \
         patch("time.sleep", lambda *a, **k: None):
        result = CliRunner().invoke(debug_mod.gdbserver, argv, obj=obj)
    return result, client, routes


# ---------------------------------------------------------------------------
# Plain box: unchanged
# ---------------------------------------------------------------------------

def test_plain_box_prints_the_box_address_and_returns():
    result, client, routes = run([])
    assert result.exit_code == 0, result.output
    assert f"target remote {BOX_IP}:2331" in result.output
    assert "localhost" not in result.output
    assert FakeTunnel.instances == []
    assert routes == [(BOX_IP, 2331)]
    assert client.calls[-1] == "close"


def test_plain_box_json_has_no_tunnel_key():
    result, _, _ = run(["--json"])
    assert result.exit_code == 0
    assert "tunnel" not in json.loads(result.stdout)


def test_the_route_uses_the_port_the_box_assigned():
    result, _, routes = run([], client=FakeClient(port=2334))
    assert routes == [(BOX_IP, 2334)]
    assert f"{BOX_IP}:2334" in result.output


# ---------------------------------------------------------------------------
# Gated box: tunnel
# ---------------------------------------------------------------------------

def test_gated_box_prints_localhost_and_never_the_box_address():
    result, _, _ = run([], route=debug_mod.ROUTE_TUNNEL)
    assert result.exit_code == 0, result.output
    assert "GDB server running on STG-1. Connect to localhost:2331" in result.output
    assert "arm-none-eabi-gdb -ex 'target remote localhost:2331'" in result.output
    assert f"{BOX_IP}:2331" not in result.output
    assert "target remote STG-1" not in result.output


def test_gated_box_stays_in_the_foreground_until_ctrl_c():
    result, client, _ = run([], route=debug_mod.ROUTE_TUNNEL)
    tunnel = FakeTunnel.instances[0]
    assert tunnel.events[:2] == ["bind", "serve_forever"]
    assert tunnel.events[-1] == "close"
    # Ctrl-C closes the tunnel only: the server is never stopped.
    assert not any(isinstance(c, tuple) and c[0] == "disconnect" for c in client.calls)
    assert "Tunnel closed. The GDB server continues to run on STG-1." in result.output
    assert "lager debug dbg1 disconnect --box STG-1" in result.output


def test_already_running_on_a_gated_box_also_prints_localhost():
    result, _, _ = run([], route=debug_mod.ROUTE_TUNNEL,
                       client=FakeClient(status="already_running"))
    assert "already running!" in result.output
    assert "Connect to localhost:2331" in result.output
    assert f"{BOX_IP}:2331" not in result.output


def test_local_port_moves_the_listener():
    result, _, _ = run(["--local-port", "3333"], route=debug_mod.ROUTE_TUNNEL)
    assert FakeTunnel.instances[0].local_port == 3333
    assert FakeTunnel.instances[0].remote_port == 2331
    assert "target remote localhost:3333" in result.output


def test_a_taken_local_port_fails_before_printing_an_address():
    class Taken(FakeTunnel):
        def bind(self):
            raise LagerError("Local port 2331 is already in use.",
                             fixes=["Pick a free local port with --local-port <PORT>."])
    result, client, _ = run([], route=debug_mod.ROUTE_TUNNEL, tunnel_cls=Taken)
    assert result.exit_code == 1
    assert "already in use" in result.output
    assert "--local-port" in result.output
    assert "Connect to" not in result.output
    assert "close" in client.calls


def test_json_on_a_gated_box_reports_the_tunnel_then_serves():
    result, _, _ = run(["--json"], route=debug_mod.ROUTE_TUNNEL)
    body = json.loads(result.stdout)
    assert body["tunnel"] == {"local_host": "127.0.0.1", "local_port": 2331,
                              "box_port": 2331}
    assert FakeTunnel.instances[0].events[:2] == ["bind", "serve_forever"]


def test_quiet_on_a_gated_box_still_serves_the_tunnel():
    result, _, _ = run(["--quiet"], route=debug_mod.ROUTE_TUNNEL)
    assert "serve_forever" in FakeTunnel.instances[0].events
    assert result.output == ""


def test_no_tunnel_returns_without_printing_an_unreachable_address():
    result, client, _ = run(["--no-tunnel"], route=debug_mod.ROUTE_TUNNEL)
    assert result.exit_code == 0
    assert FakeTunnel.instances == []
    assert f"{BOX_IP}:2331" not in result.output
    assert "without --no-tunnel" in result.output
    assert client.calls[-1] == "close"


def test_a_fatal_tunnel_refusal_exits_nonzero():
    class Revoked(FakeTunnel):
        def serve_forever(self):
            raise LagerError("You are signed in but not authorized to use box 10.9.8.7.")
    result, _, _ = run([], route=debug_mod.ROUTE_TUNNEL, tunnel_cls=Revoked)
    assert result.exit_code == 1
    assert "not authorized to use box" in result.output


def test_rtt_on_a_gated_box_serves_the_tunnel_alongside_the_stream():
    result, client, _ = run(["--rtt"], route=debug_mod.ROUTE_TUNNEL)
    tunnel = FakeTunnel.instances[0]
    assert "start" in tunnel.events and "serve_forever" not in tunnel.events
    assert tunnel.events[-1] == "close"
    assert "rtt" in client.calls
    assert "Connect to localhost:2331" in result.output


def test_interactive_rtt_on_a_gated_box_closes_the_tunnel_on_exit():
    result, _, _ = run(["--rtt", "--interactive"], route=debug_mod.ROUTE_TUNNEL)
    assert result.exit_code == 0
    assert FakeTunnel.instances[0].events[-1] == "close"


def test_reset_on_a_gated_box_resets_then_serves():
    result, client, _ = run(["--reset"], route=debug_mod.ROUTE_TUNNEL)
    assert "reset" in client.calls
    assert "serve_forever" in FakeTunnel.instances[0].events


# ---------------------------------------------------------------------------
# Gated box whose gateway cannot tunnel
# ---------------------------------------------------------------------------

OLD_GATEWAY = LagerError(
    f"Port 2331 on box {BOX_IP} is reachable only through its gateway, and "
    "that gateway does not support debug tunnels yet.")


def test_an_old_gateway_fails_the_command_without_a_box_address():
    result, _, _ = run([], route=OLD_GATEWAY)
    assert result.exit_code == 1
    assert "does not support debug tunnels yet" in result.output
    assert f"target remote {BOX_IP}" not in result.output


def test_an_old_gateway_does_not_stop_an_rtt_stream():
    # RTT streams over HTTP, which the gateway already forwards.
    result, client, _ = run(["--rtt"], route=OLD_GATEWAY)
    assert "does not support debug tunnels yet" in result.output
    assert "rtt" in client.calls


# ---------------------------------------------------------------------------
# disconnect --keep-server
# ---------------------------------------------------------------------------

def _keep_server(gated):
    obj = _Obj()
    obj.net_name = NET["name"]
    with patch.object(debug_mod, "_resolve_box_with_username",
                      lambda ctx, b: (BOX_IP, "lagerdata")), \
         patch.object(debug_mod, "_get_debug_net",
                      lambda ctx, b, net_name=None: NET), \
         patch.object(debug_mod, "_get_service_client", lambda b: FakeClient()), \
         patch.object(debug_mod, "auth_server_for_box",
                      lambda b: "http://cp:3001" if gated else None), \
         patch.object(debug_mod, "choose_route",
                      MagicMock(side_effect=AssertionError("probed"))):
        return CliRunner().invoke(debug_mod.disconnect,
                                  ["--keep-server", "--box", "STG-1"], obj=obj)


def test_keep_server_on_a_plain_box_prints_the_box_address():
    result = _keep_server(gated=False)
    assert f"target extended-remote {BOX_IP}:2331" in result.output


def test_keep_server_on_a_gated_box_points_at_the_tunnel_without_probing():
    result = _keep_server(gated=True)
    assert result.exit_code == 0, result.output
    assert f"{BOX_IP}:2331" not in result.output
    assert "lager debug dbg1 gdbserver --box STG-1" in result.output
