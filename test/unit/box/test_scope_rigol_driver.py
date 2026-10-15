# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
A Rigol scope net's drivers, both of which reach the scope through its
daemon: the scope driver every surface uses, and the SCPI layer under the
logic analyzer, bus decoders and protocol triggers.
"""
from __future__ import annotations

from unittest import mock

import pytest

from lager.measurement.scope import daemon_client, rigol_daemons, rigol_mso5000
from lager.measurement.scope.picoscope import PicoScope
from lager.measurement.scope.rigol_scope import RigolScope

ADDRESS = "USB0::0x1AB1::0x0515::MS5A244000001::INSTR"


class FakeClient:
    """Answers `Scpi` as the daemon does: a query's reply, or null."""

    def __init__(self, url=None, timeout=None):
        self.url = url
        self.sent = []

    def command(self, name, timeout=None, **params):
        self.sent.append((name, params))
        if name == "Scpi":
            command = params["scpi"]
            return {"reply": "1" if command.endswith("?") else None}
        return {}

    def close(self):
        pass


@pytest.fixture
def daemon(monkeypatch):
    clients = []

    def make(url=None, timeout=None):
        client = FakeClient(url, timeout)
        clients.append(client)
        return client

    monkeypatch.setattr(daemon_client, "ScopeDaemonClient", make)
    monkeypatch.setattr(rigol_daemons, "ensure",
                        lambda address, video=None: "ws://rigol/%s/%s" % (address, video))
    monkeypatch.setattr(rigol_daemons, "capture_for_address", lambda address: "usb:1-1.3")
    return clients


class TestTheScopeDriver:

    def test_it_talks_to_the_daemon_for_its_address(self, daemon):
        scope = RigolScope(address=ADDRESS, pin=2, netname="rigol ch2",
                           record={"instrument": "Rigol_MSO5204", "address": ADDRESS})
        scope.capabilities()
        assert daemon[0].url == "ws://rigol/%s/usb:1-1.3" % ADDRESS

    def test_autoscale_waits_for_the_scope_to_finish(self, daemon):
        scope = RigolScope(address=ADDRESS)
        scope.autoscale()
        assert [p["scpi"] for _, p in daemon[0].sent] == [":AUToscale", "*OPC?"]

    @pytest.mark.parametrize("given,token", [
        ("dc", "DC"), ("AC", "AC"), ("low_freq_rej", "LFReject"),
        ("high_freq_rej", "HFReject"),
    ])
    def test_trigger_coupling_is_the_scope_s_filter(self, daemon, given, token):
        RigolScope(address=ADDRESS).set_trigger_coupling(given)
        assert daemon[0].sent == [("Scpi", {"scpi": ":TRIGger:COUPling %s" % token})]

    def test_an_unknown_trigger_coupling_changes_nothing(self, daemon):
        with pytest.raises(ValueError, match="trigger coupling"):
            RigolScope(address=ADDRESS).set_trigger_coupling("gnd")
        assert daemon == []

    def test_two_nets_on_one_scope_share_its_cursors(self):
        a = RigolScope(address=ADDRESS, pin=1)
        b = RigolScope(address=ADDRESS, pin=2)
        assert a._instrument_key() == b._instrument_key()


class TestTheScpiLayer:

    def test_writes_and_queries_go_through_the_daemon(self, daemon, monkeypatch):
        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        rigol = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=1)
        rigol.write(":LA:STATe ON")
        assert rigol.query(":LA:STATe?") == "1"
        assert [p["scpi"] for _, p in daemon[0].sent] == [":LA:STATe ON", ":LA:STATe?"]

    def test_an_enum_from_the_proxy_is_sent_as_its_token(self, daemon, monkeypatch):
        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        rigol = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=1)
        rigol.set_measurement_source({"__enum__": {"type": "MeasurementSource", "value": "D1"}})
        rigol.set_cursor_mode({"__enum__": {"type": "CursorMode", "value": "MANual"}})
        # A digital channel is measured only while it is shown.
        assert [p["scpi"] for _, p in daemon[0].sent] == [
            ":LA:STATe ON", ":LA:DISPlay D1,ON", ":MEASure:SOURce D1",
            ":CURSor:MODE MANual"]

    def test_every_net_on_a_scope_shares_one_connection(self, daemon, monkeypatch):
        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        first = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=1)
        second = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=2)
        first.write(":RUN")
        second.write(":STOP")
        assert len(daemon) == 1

    def test_a_restarted_daemon_is_reconnected_once(self, monkeypatch):
        calls = []

        class Flaky(FakeClient):
            def command(self, name, timeout=None, **params):
                calls.append(self)
                if len(calls) == 1:
                    raise daemon_client.ScopeDaemonUnavailable("restarted")
                return super().command(name, timeout=timeout, **params)

        monkeypatch.setattr(daemon_client, "ScopeDaemonClient", Flaky)
        monkeypatch.setattr(rigol_daemons, "ensure", lambda address, video=None: "ws://r/")
        monkeypatch.setattr(rigol_daemons, "capture_for_address", lambda address: None)
        instrument = rigol_mso5000.DaemonInstrument(ADDRESS)
        assert instrument.query("*IDN?") == "1"
        assert calls[0] is not calls[1], "the second try was on a new connection"


class TestTheFactory:

    def _device(self, instrument):
        from lager import scope_hs
        rec = {"name": "n", "role": "scope", "instrument": instrument, "address": ADDRESS}
        with mock.patch.object(scope_hs, "_resolve_net", return_value=rec):
            return scope_hs.create_device({"name": "n", "instrument": instrument})

    def test_a_rigol_net_gets_the_daemon_driver(self):
        device = self._device("Rigol_MSO5204")
        assert isinstance(device, RigolScope)
        assert device.address == ADDRESS

    def test_a_picoscope_net_gets_the_picoscope_driver(self):
        device = self._device("picoscope_2000")
        assert type(device) is PicoScope


class TestTheCursors:
    """The MSO5000's manual cursors: pixels on its 1000 x 480 grid, and one
    type, time or amplitude, at a time."""

    def _cursors(self, daemon, monkeypatch, net_type="analog"):
        from types import SimpleNamespace

        from lager.nets.constants import NetType
        from lager.nets.mappers.rigol_mso5000 import Cursor_RigolMSO5000FunctionMapper

        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        device = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=1)
        kind = NetType.Analog if net_type == "analog" else NetType.Logic
        net = SimpleNamespace(type=kind, channel=1)
        return Cursor_RigolMSO5000FunctionMapper(None, net, device)

    def _sent(self, daemon):
        return [p["scpi"] for _, p in daemon[0].sent]

    def test_a_cursor_is_placed_from_the_centre_of_the_grid(self, daemon, monkeypatch):
        self._cursors(daemon, monkeypatch).set_a(x=100, y=60)
        assert self._sent(daemon) == [
            ":CURSor:MODE MANual", ":CURSor:MANual:SOURce CHANnel1",
            ":CURSor:MANual:TYPE TIME", ":CURSor:MANual:CAX 600",
            ":CURSor:MODE MANual", ":CURSor:MANual:SOURce CHANnel1",
            ":CURSor:MANual:TYPE AMPLitude", ":CURSor:MANual:CAY 180",
        ]

    def test_a_logic_net_puts_the_cursors_on_the_logic_analyzer(self, daemon, monkeypatch):
        self._cursors(daemon, monkeypatch, "logic").set_b(x=-200)
        assert ":CURSor:MANual:SOURce LA" in self._sent(daemon)
        assert ":CURSor:MANual:CBX 300" in self._sent(daemon)

    def test_a_move_starts_from_where_the_cursor_was_put(self, daemon, monkeypatch):
        cursors = self._cursors(daemon, monkeypatch)
        cursors.set_a(x=10)
        cursors.move_a(x_del=-30)
        assert self._sent(daemon)[-1] == ":CURSor:MANual:CAX 480"

    def test_a_position_off_the_grid_is_refused_before_anything_is_sent(self, daemon, monkeypatch):
        with pytest.raises(ValueError, match="off the grid"):
            self._cursors(daemon, monkeypatch).set_a(x=600)
        assert all(client.sent == [] for client in daemon)

    def test_older_rigol_cursor_types_are_translated(self, daemon, monkeypatch):
        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        rigol = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=1)
        rigol.set_cursor_manual_type("XY")
        rigol.set_cursor_manual_type("Y")
        assert self._sent(daemon) == [":CURSor:MANual:TYPE TIME",
                                      ":CURSor:MANual:TYPE AMPLitude"]
