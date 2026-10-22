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
from lager.measurement.scope.picoscope import PicoScope, UnsupportedScopeFeature
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
        if name == "Measure":
            return {"measurements": {"vpp": 3.3, "frequency": 1000.0}}
        return {}

    def captures(self, timeout=None):
        self.sent.append(("Subscribe", {}))
        while True:
            yield "frame"

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

    def test_moving_the_time_position_does_not_start_a_stopped_scope(self, daemon):
        # The PicoScope's re-arm restarted a stopped Rigol and lost its capture.
        RigolScope(address=ADDRESS).set_timebase_offset(0.001)
        assert [n for n, _ in daemon[0].sent] == ["SetTimeOffset", "GetTimeOffset"]
        assert daemon[0].sent[0] == ("SetTimeOffset", {"time_offset": 0.001})

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


class TestAcquisitionGoesThroughTheDaemon:
    """Raw :RUN ran the scope but left the daemon publishing nothing."""

    def _scope(self, daemon, monkeypatch, mode="auto"):
        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        scope = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=1)
        original = FakeClient.command

        def command(client, name, timeout=None, **params):
            if name == "GetCaptureMode":
                client.sent.append((name, params))
                return {"capture_mode": mode}
            return original(client, name, timeout=timeout, **params)
        monkeypatch.setattr(FakeClient, "command", command)
        return scope

    def test_run_starts_the_daemon_acquiring(self, daemon, monkeypatch):
        self._scope(daemon, monkeypatch).run()
        assert [n for n, _ in daemon[0].sent] == ["GetCaptureMode", "StartAcquisition"]

    def test_run_from_single_runs_in_auto(self, daemon, monkeypatch):
        self._scope(daemon, monkeypatch, mode="single").run()
        assert daemon[0].sent[1] == ("SetCaptureMode", {"capture_mode": "auto"})

    def test_stop_single_and_force_are_daemon_commands(self, daemon, monkeypatch):
        scope = self._scope(daemon, monkeypatch)
        scope.stop()
        scope.single()
        scope.trigger_force()
        assert [n for n, _ in daemon[0].sent] == [
            "StopAcquisition", "SetCaptureMode", "StartAcquisition", "ForceTrigger"]


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


class TestTheFullMeasurementSet:

    def test_it_is_the_daemons_set_for_the_nets_channel(self, daemon, monkeypatch):
        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        rigol = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=2)
        assert rigol.measure_all() == {"vpp": 3.3, "frequency": 1000.0}
        assert daemon[0].sent == [("Measure", {"channel": {"Alphabetic": "B"}})]

    def test_the_set_gets_longer_than_one_command(self, monkeypatch):
        # 14 queries at ~330 ms each ran past the 5 s a command gets.
        timeouts = []

        class Timed(FakeClient):
            def command(self, name, timeout=None, **params):
                timeouts.append((name, timeout))
                return super().command(name, timeout=timeout, **params)

        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        monkeypatch.setattr(daemon_client, "ScopeDaemonClient", Timed)
        monkeypatch.setattr(rigol_daemons, "ensure", lambda address, video=None: "ws://r/")
        monkeypatch.setattr(rigol_daemons, "capture_for_address", lambda address: None)
        scope = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=1)
        scope.measure_all()
        scope.write(":RUN")
        assert timeouts == [("Measure", rigol_mso5000.MEASURE_ALL_TIMEOUT_S), ("Scpi", 5.0)]

    def test_an_explicit_channel_wins(self, daemon, monkeypatch):
        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        rigol_mso5000.RigolMso5000(address=ADDRESS, pin=2).measure_all(4)
        assert daemon[0].sent == [("Measure", {"channel": {"Alphabetic": "D"}})]


class TestOneMeasurementItem:
    """Items read one at a time over SCPI, as `measurement.<item>()` does."""

    @pytest.mark.parametrize("item", [
        "MPARea", "VAVerage", "RRDelay", "RFDelay", "FRDelay", "FFDelay",
        "RRPHase", "RFPHase", "FRPHase", "FFPHase",
        {"__enum__": {"type": "MeasurementItem", "value": "MPARea"}},
    ])
    def test_an_item_the_scope_rejects_is_refused_before_anything_is_sent(self, daemon, monkeypatch, item):
        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        rigol = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=1)
        with pytest.raises(UnsupportedScopeFeature, match="rejects :MEASure:"):
            rigol.get_measure_item(item)
        assert not any(name == "Scpi" for client in daemon for name, _ in client.sent)

    def test_the_average_is_asked_by_the_header_the_scope_answers(self, daemon, monkeypatch):
        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        rigol = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=1)
        rigol.measure_vavg(1)
        assert [p["scpi"] for _, p in daemon[0].sent] == [":MEASure:SOURce CHANnel1", ":MEASure:VAVG?"]

    def test_an_item_is_asked_alone_and_nothing_is_cleared(self, daemon, monkeypatch):
        # The daemon removes the readout the query adds; clearing ALL here
        # would take the user's own readouts with it.
        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        rigol = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=1)
        assert rigol.get_measure_item("VPP") == 1.0
        assert [p["scpi"] for _, p in daemon[0].sent] == [":MEASure:VPP?"]

    def test_a_readout_kept_on_screen_is_for_the_source_set(self, daemon, monkeypatch):
        monkeypatch.setattr(rigol_mso5000, "_instruments", {})
        rigol = rigol_mso5000.RigolMso5000(address=ADDRESS, pin=1)
        rigol.set_measurement_source("CHANnel2")
        rigol.show_measurement({"__enum__": {"type": "MeasurementItem", "value": "FREQuency"}})
        assert [p["scpi"] for _, p in daemon[0].sent] == [
            ":MEASure:SOURce CHANnel2", ":MEASure:ITEM FREQuency,CHANnel2"]


class TestStreamingNeedsALiveView:

    def _scope(self, record=None):
        return RigolScope(address=ADDRESS, pin=1,
                          record=record or {"instrument": "Rigol_MSO5204", "address": ADDRESS})

    def test_without_one_it_is_refused_before_connecting(self, daemon, monkeypatch):
        monkeypatch.setattr(rigol_daemons, "capture_for_address", lambda address: None)
        scope = self._scope()
        with pytest.raises(UnsupportedScopeFeature, match="set-capture"):
            scope.stream_frames(count=1)
        with pytest.raises(UnsupportedScopeFeature, match="set-capture"):
            scope.stream_capture(duration=0.1)
        assert daemon == []

    def test_screenshots_count_as_a_live_view(self, daemon, monkeypatch):
        monkeypatch.setattr(rigol_daemons, "capture_for_address", lambda address: None)
        scope = self._scope({"instrument": "Rigol_MSO5204", "address": ADDRESS,
                             "capture": {"device": "screen"}})
        assert list(scope.stream_frames(count=2)) == ["frame", "frame"]

    def test_a_timeout_says_why_there_is_no_picture(self, daemon, monkeypatch):
        problem = "pairing the MSO5074 with the capture card: no picture"

        def captures(client, timeout=None):
            raise daemon_client.ScopeDaemonTimeout("no capture arrived in 1 s")
            yield  # a generator, as the real one is

        original = FakeClient.command

        def command(client, name, timeout=None, **params):
            if name == "GetState":
                return {"state": {"live_view_problem": problem}}
            return original(client, name, timeout=timeout, **params)

        monkeypatch.setattr(FakeClient, "captures", captures)
        monkeypatch.setattr(FakeClient, "command", command)
        frames = self._scope().stream_frames(count=1, timeout=1)
        with pytest.raises(daemon_client.ScopeDaemonTimeout, match="no live view: pairing"):
            list(frames)

    def test_a_single_capture_needs_no_live_view(self, daemon, monkeypatch):
        monkeypatch.setattr(rigol_daemons, "capture_for_address", lambda address: None)
        scope = self._scope()
        scope.client.capture = lambda timeout=None: "record"
        assert scope.capture() == "record"


class TestTheNetApi:
    """`Net.get(<rigol net>)`: the calls a PicoScope net already answered."""

    def _mapper(self, net_type="analog"):
        from types import SimpleNamespace

        from lager.nets.constants import NetType
        from lager.nets.mappers.rigol_mso5000 import RigolMSO5000AnalogMapper

        kind = NetType.Analog if net_type == "analog" else NetType.Logic
        net = SimpleNamespace(type=kind, channel=3, name="sda")
        device = mock.MagicMock()
        device.net_info = {"instrument": "Rigol_MSO5204", "address": ADDRESS,
                           "capture": {"device": "screen"}}
        return RigolMSO5000AnalogMapper(net, device), device

    def test_all_reads_the_nets_channel_through_the_device(self):
        mapper, device = self._mapper()
        device.measure_all.return_value = {"vpp": 1.0}
        assert mapper.measurement.all() == {"vpp": 1.0}
        device.measure_all.assert_called_once_with(3)

    def test_all_on_a_logic_net_is_refused(self):
        mapper, device = self._mapper("logic")
        with pytest.raises(UnsupportedScopeFeature, match="analog"):
            mapper.measurement.all()
        device.measure_all.assert_not_called()

    def test_all_on_the_instrument_net_is_refused(self):
        mapper, device = self._mapper()
        device.net_info["role"] = "scope"
        with pytest.raises(UnsupportedScopeFeature, match="is the scope itself"):
            mapper.measurement.all()
        device.measure_all.assert_not_called()

    @pytest.mark.parametrize("method", [
        "waveform_period_area",
        "delay_rising_rising_edge", "delay_rising_falling_edge",
        "delay_falling_rising_edge", "delay_falling_falling_edge",
        "phase_rising_rising_edge", "phase_rising_falling_edge",
        "phase_falling_rising_edge", "phase_falling_falling_edge",
    ])
    def test_a_measurement_the_scope_rejects_sends_nothing(self, method):
        mapper, device = self._mapper()
        with pytest.raises(UnsupportedScopeFeature, match="rejects"):
            getattr(mapper.measurement, method)()
        assert device.method_calls == []

    @pytest.mark.parametrize("kind,name", [
        ("UART", "UART"), ("I2C", "I2C"), ("Pulse", "pulse"), ("CAN", "can")])
    def test_a_trigger_type_with_no_setters_is_refused_before_anything_is_sent(self, kind, name):
        from lager.nets.defines import TriggerType
        mapper, device = self._mapper()
        with pytest.raises(UnsupportedScopeFeature, match="edge and SPI triggers.*not %s" % name):
            mapper.trigger_settings.set_type(getattr(TriggerType, kind))
        assert device.method_calls == []

    @pytest.mark.parametrize("call", [
        lambda t, net: t.uart.set_source(net),
        lambda t, net: t.uart.set_uart_params(baud=9600),
        lambda t, net: t.uart.set_trigger_on_start(),
        lambda t, net: t.pulse.set_level(1.0),
        lambda t, net: t.pulse.set_trigger_on_pulse_greater_than_width(1e-6),
        lambda t, net: t.i2c.set_trigger_on_start(),
        lambda t, net: t.can.set_source(net),
    ])
    def test_a_protocol_trigger_s_setters_send_nothing(self, call):
        mapper, device = self._mapper()
        with pytest.raises(UnsupportedScopeFeature, match="edge and SPI triggers"):
            call(mapper.trigger_settings, mapper.net)
        assert device.method_calls == []

    def test_edge_and_spi_triggers_still_reach_the_scope(self):
        from lager.nets.defines import TriggerType
        mapper, device = self._mapper()
        mapper.trigger_settings.set_type(TriggerType.Edge)
        mapper.trigger_settings.set_type(TriggerType.SPI)
        assert [c[0] for c in device.method_calls] == ["set_trigger_type", "set_trigger_type"]

    def test_a_measurement_leaves_the_users_readouts(self):
        mapper, device = self._mapper()
        device.get_measure_item.return_value = 3.3
        assert mapper.measurement.voltage_peak_to_peak() == 3.3
        called = [c[0] for c in device.method_calls]
        assert "clear_measurement" not in called, called
        assert "show_measurement" not in called, called

    def test_a_displayed_measurement_stays_on_the_screen(self):
        mapper, device = self._mapper()
        device.get_measure_item.return_value = 334.6
        mapper.measurement.frequency(display=True)
        device.show_measurement.assert_called_once()

    def test_samples_come_from_a_driver_in_this_process(self, monkeypatch):
        from lager.measurement.scope import rigol_scope
        made = []
        local = mock.MagicMock()

        def make(**kwargs):
            made.append(kwargs)
            return local

        monkeypatch.setattr(rigol_scope, "RigolScope", make)
        mapper, device = self._mapper()
        mapper.stream_start(channel="C", time_per_div=0.001)
        mapper.capture(timeout=2.0)
        mapper.stream_frames(count=3)
        mapper.stream_capture(output="run.csv", duration=5.0)
        mapper.stream_stop()
        local.stream_start.assert_called_once_with(channel="C", time_per_div=0.001)
        local.stream_stop.assert_called_once_with()
        local.capture.assert_called_once_with(timeout=2.0)
        local.stream_frames.assert_called_once_with(count=3, timeout=None)
        local.stream_capture.assert_called_once_with(
            output="run.csv", duration=5.0, samples=None, timeout=None)
        assert made == [{"address": ADDRESS, "pin": 3, "netname": "sda",
                         "record": device.net_info}]
        mapper.close_capture_connection()
        local.close.assert_called_once_with()
        device.capture.assert_not_called()
        device.close.assert_not_called()


class TestARepairedScope:
    """`lager nets set-capture` under a driver the hardware service kept."""

    def test_the_scope_driver_follows_the_saved_pairing(self, daemon, monkeypatch):
        from lager.nets.net import Net
        saved = [{"name": "rigol1", "role": "scope", "instrument": "Rigol_MSO5204",
                  "address": ADDRESS, "capture": {"device": "screen"}}]
        monkeypatch.setattr(Net, "get_local_nets", staticmethod(lambda: saved))
        monkeypatch.setattr(rigol_daemons, "capture_for_address", lambda address: None)
        scope = RigolScope(address=ADDRESS, netname="rigol1", record=dict(saved[0]))
        scope.capabilities()
        saved[0] = dict(saved[0], capture={"port_path": "3-3"})
        scope.capabilities(refresh=True)
        assert [c.url for c in daemon] == ["ws://rigol/%s/screen" % ADDRESS,
                                           "ws://rigol/%s/usb:3-3" % ADDRESS]

    def test_an_unchanged_pairing_keeps_its_connection(self, daemon, monkeypatch):
        from lager.nets.net import Net
        saved = [{"name": "rigol1", "role": "scope", "instrument": "Rigol_MSO5204",
                  "address": ADDRESS, "capture": {"device": "screen"}}]
        monkeypatch.setattr(Net, "get_local_nets", staticmethod(lambda: saved))
        scope = RigolScope(address=ADDRESS, netname="rigol1", record=dict(saved[0]))
        scope.capabilities()
        scope.capabilities(refresh=True)
        assert len(daemon) == 1

    def test_the_scpi_layer_follows_it_too(self, daemon, monkeypatch):
        pairing = ["screen"]
        monkeypatch.setattr(rigol_daemons, "capture_for_address", lambda address: pairing[0])
        instrument = rigol_mso5000.DaemonInstrument(ADDRESS)
        instrument.write(":RUN")
        pairing[0] = "usb:3-3"
        instrument.write(":STOP")
        assert [c.url for c in daemon] == ["ws://rigol/%s/screen" % ADDRESS,
                                           "ws://rigol/%s/usb:3-3" % ADDRESS]

    def test_a_restarted_daemon_is_looked_up_again(self, monkeypatch):
        # The daemon restarted on a new port (a power-cycled scope, a stopped
        # daemon): the driver the hardware service kept must not keep dialing
        # the old one.
        ports = iter([52087, 38979])
        urls = []

        class Gone(FakeClient):
            def command(self, name, timeout=None, **params):
                if self.url.endswith(":52087/"):
                    raise daemon_client.ScopeDaemonUnavailable("connection refused")
                return super().command(name, timeout=timeout, **params)

        def ensure(address, video=None):
            urls.append("ws://127.0.0.1:%d/" % next(ports))
            return urls[-1]

        monkeypatch.setattr(daemon_client, "ScopeDaemonClient", Gone)
        monkeypatch.setattr(rigol_daemons, "ensure", ensure)
        monkeypatch.setattr(rigol_daemons, "capture_for_address", lambda address: None)
        scope = RigolScope(address=ADDRESS, pin=1)
        assert scope.scpi(":MEASure:VAVG?") == "1"
        assert urls == ["ws://127.0.0.1:52087/", "ws://127.0.0.1:38979/"]
