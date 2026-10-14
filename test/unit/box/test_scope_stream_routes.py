# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the scope stream routes (box/lager/http_handlers/scope.py).

The ticket names the WebSocket to open, and the relay behind it reaches the
daemon driving the net's scope: the box's own for a PicoScope, one of its own
for a Rigol. So the ticket is the place to refuse a net the relay cannot
serve, and the path it hands back has to be a working URL whatever the net
is called.
"""

import socket
import types

import flask
import pytest

from lager.http_handlers import scope
from lager.measurement.scope import daemon_client, rigol_daemons
from lager.nets.net import Net

NETS = [
    {"name": "pico1", "role": "scope", "instrument": "picoscope_2000"},
    {"name": "vbus", "role": "scope-channel", "instrument": "picoscope_2000", "pin": 1},
    {"name": "bench vbus", "role": "scope-channel", "instrument": "picoscope_2000", "pin": 2},
    {"name": "rigol1", "role": "scope", "instrument": "Rigol_MSO5204",
     "address": "USB0::0x1AB1::0x0515::MS5A1::INSTR"},
    {"name": "rigol2", "role": "scope", "instrument": "Rigol_MSO5204",
     "address": "USB0::0x1AB1::0x0515::MS5A2::INSTR",
     "capture": {"vid": "534d", "pid": "2109", "port_path": "1-1.3"}},
    {"name": "rigol2 ch1", "role": "scope-channel", "instrument": "Rigol_MSO5204",
     "address": "USB0::0x1AB1::0x0515::MS5A2::INSTR", "pin": 1},
    {"name": "keysight1", "role": "scope", "instrument": "Keysight_DSOX1204G"},
    {"name": "gpio1", "role": "gpio", "instrument": "labjack_t7"},
]

RIGOL_DAEMON = "ws://127.0.0.1:9999/"


@pytest.fixture
def asked():
    return []


@pytest.fixture
def client(monkeypatch, asked):
    monkeypatch.setattr(Net, "get_local_nets", staticmethod(lambda: NETS))

    def command(name, timeout=None, url=None, **params):
        asked.append(url)
        return {"capabilities": {"model": "2204A"}}

    monkeypatch.setattr(daemon_client, "command", command)
    monkeypatch.setattr(rigol_daemons, "ensure",
                        lambda address, video=None: RIGOL_DAEMON + "?" + str(video))
    app = flask.Flask(__name__)
    scope.register_scope_routes(app)
    return app.test_client()


class TestTicket:

    @pytest.mark.parametrize("net", ["pico1", "vbus"])
    def test_the_scope_and_its_channels_get_one(self, client, net):
        response = client.get("/scope/%s/stream" % net)
        assert response.status_code == 200
        assert response.get_json()["success"] is True

    @pytest.mark.parametrize("net", ["rigol2", "rigol2 ch1"])
    def test_a_rigol_with_a_capture_card_streams_from_its_own_daemon(
            self, client, asked, net):
        response = client.get("/scope/%s/stream" % net)
        assert response.status_code == 200, response.get_json()
        # A channel net finds the card through the scope net it shares an
        # address with.
        assert asked == [RIGOL_DAEMON + "?usb:1-1.3"]

    def test_a_picoscope_streams_from_the_box_daemon(self, client, asked):
        client.get("/scope/pico1/stream")
        assert asked == [daemon_client.daemon_url()]

    def test_a_rigol_without_a_capture_card_is_refused_and_told_why(self, client):
        response = client.get("/scope/rigol1/stream")
        assert response.status_code == 400
        assert "capture card" in response.get_json()["error"]

    def test_a_scope_no_daemon_drives_is_refused_and_told_why(self, client):
        response = client.get("/scope/keysight1/stream")
        assert response.status_code == 400
        assert "no oscilloscope daemon" in response.get_json()["error"]

    def test_a_net_that_is_not_a_scope_is_not_found(self, client):
        assert client.get("/scope/gpio1/stream").status_code == 404

    def test_the_websocket_path_is_a_url_whatever_the_net_is_called(self, client):
        ticket = client.get("/scope/bench%20vbus/stream").get_json()
        assert ticket["ws_path"] == "/scope/bench%20vbus/ws?token=" + ticket["token"]

    def test_the_ticket_redeems_under_the_name_the_relay_route_receives(self, client):
        # Flask decodes the path, so the relay sees the name unquoted.
        ticket = client.get("/scope/bench%20vbus/stream").get_json()
        assert scope._redeem_ticket(ticket["token"], "bench vbus")


class TestKeepAlive:

    def test_the_browser_connection_is_probed_when_idle(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            scope._keep_alive(types.SimpleNamespace(sock=sock))
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            if hasattr(socket, "TCP_KEEPIDLE"):
                assert (sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE)
                        == scope._KEEPALIVE_IDLE_SECONDS)

    def test_a_socket_without_tcp_options_is_left_alone(self):
        left, right = socket.socketpair()
        with left, right:
            scope._keep_alive(types.SimpleNamespace(sock=left), types.SimpleNamespace())
