# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The box handler's actions for the settings a PicoScope has the box do:
status, trigger readback, acquisition, holdoff, roll, display and spectrum.

What is pinned here is the handler's part -- which nets take the action,
which parameters it requires, and the sentence it answers with -- against a
mock driver. A Rigol does all of this on its own front panel, and before the
PicoScope-only gate a Rigol net answered with the driver's missing
attribute, which read as a bug rather than as the answer.
"""
from __future__ import annotations

from unittest import mock

import pytest

from lager.http_handlers import net_command

NETS = [
    {"name": "pico1", "role": "scope", "instrument": "picoscope_2000",
     "address": "usb::one"},
    {"name": "rigol1", "role": "scope", "instrument": "Rigol_MSO5204",
     "address": "usb::two"},
    {"name": "rigol_ch2", "role": "scope-channel", "instrument": "Rigol_MSO5204",
     "address": "usb::two", "pin": 2},
]

PICOSCOPE_ONLY = [
    ("get_state", {}),
    ("set_acquire", {"mode": "average", "count": 16}),
    ("get_acquire", {}),
    ("set_trigger_holdoff", {"seconds": 1e-3}),
    ("get_trigger_holdoff", {}),
    ("set_roll", {"mode": "on"}),
    ("get_roll", {}),
    ("set_display", {"persistence": 2}),
    ("get_display", {}),
    ("fft", {"channel": "A"}),
    ("capabilities", {}),
    ("set_cursor", {"time": [0.0, 1e-3]}),
    ("get_cursor", {}),
    ("clear_cursor", {}),
    ("measure_cursor", {}),
]

# Everything a Rigol channel net is not refused, with parameters it accepts.
RIGOL_ACTIONS = [
    ("enable_net", {}), ("disable_net", {}), ("get_net_enabled", {}),
    ("start_capture", {}), ("start_single", {}), ("stop_capture", {}),
    ("force_trigger", {}), ("autoscale", {}),
    ("set_scale", {"volts_per_div": 0.5}), ("get_scale", {}),
    ("set_timebase", {"seconds_per_div": 1e-3}), ("get_timebase", {}),
    ("set_coupling", {"mode": "AC"}), ("get_coupling", {}),
    ("set_probe", {"ratio": 10}), ("get_probe", {}),
    ("set_offset", {"offset": 0.1}), ("get_offset", {}),
    ("set_time_offset", {"offset": 1e-4}), ("get_time_offset", {}),
    ("trigger_edge", {"source": "CHANnel2", "slope": "POSitive",
                      "coupling": "DC", "level": 0.5, "mode": "normal"}),
    ("get_capture_mode", {}), ("get_trigger_source", {}),
    ("get_trigger_slope", {}), ("get_trigger_level", {}), ("get_trigger", {}),
] + [(action, {}) for action in sorted(net_command._SCOPE_MEASUREMENTS)]

STATE = {
    "acquiring": True, "rolling": False, "capture_mode": "auto",
    "timebase": {"time_per_div": 1e-3, "time_offset": 0.0},
    "trigger": {"source": "A", "slope": "rising", "level": 0.5},
    "acquisition": {"mode": "average", "average_count": 16},
    "channels": [
        {"channel": "A", "enabled": True, "volts_per_div": 1.0,
         "coupling": "DC", "attenuation": 10.0},
        {"channel": "B", "enabled": False, "volts_per_div": 0.5,
         "coupling": "AC", "attenuation": 1.0},
    ],
}


def _run(netname, action, params=None, device=None, role="scope"):
    device = device if device is not None else mock.MagicMock()
    with mock.patch.object(net_command, "Net") as NetMock, \
            mock.patch.object(net_command, "_proxy", return_value=device):
        NetMock.get_local_nets.return_value = NETS
        return net_command._scope(netname, role, action, dict(params or {}))


class TestOnlyAPicoScopeNetTakesThem:

    @pytest.mark.parametrize("action,params", PICOSCOPE_ONLY)
    def test_a_rigol_net_is_told_to_use_its_front_panel(self, action, params):
        device = mock.MagicMock()
        with pytest.raises(net_command.WrongNetForAction, match="front panel"):
            _run("rigol1", action, params, device)
        # Refused before anything reached the instrument.
        assert device.method_calls == []

    def test_the_trigger_readback_works_on_a_rigol_without_holdoff(self):
        device = mock.MagicMock()
        device.get_capture_mode.return_value = "normal"
        device.get_trigger_source.return_value = "CH1"
        device.get_trigger_slope.return_value = "rising"
        device.get_trigger_level.return_value = 1.5
        result = _run("rigol1", "get_trigger", {}, device)
        assert result["value"] == {"mode": "normal", "source": "CH1",
                                   "slope": "rising", "level": 1.5}
        device.get_trigger_holdoff.assert_not_called()

    def test_measure_all_on_a_rigol_channel_is_told_to_use_its_front_panel(self):
        device = mock.MagicMock()
        with pytest.raises(net_command.WrongNetForAction, match="front panel"):
            _run("rigol_ch2", "measure_all", {}, device, role="scope-channel")
        assert device.method_calls == []


class TestARigolNetReachesOnlyWhatItsDriverHas:
    """Whatever the gates let through reaches the Rigol driver by name.

    A name it lacks failed on the box as a 502 with "Function not found",
    which is how the trigger readback and the enabled readback failed.
    """

    @pytest.mark.parametrize("action,params", RIGOL_ACTIONS)
    def test_every_call_is_one_the_driver_has(self, action, params):
        from lager.measurement.scope.rigol_mso5000 import RigolMso5000

        device = mock.create_autospec(RigolMso5000, instance=True)
        device.get_measure_item.return_value = 1.0
        _run("rigol_ch2", action, params, device, role="scope-channel")


class TestARigolMeasurement:
    """The measurements went out under the daemon's names, which the Rigol
    does not have, and with no source, so they read whichever channel the
    last caller left selected."""

    def test_it_asks_by_the_rigol_name_on_the_net_channel(self):
        device = mock.MagicMock()
        device.get_measure_item.return_value = 1000.0
        result = _run("rigol_ch2", "measure_freq", {}, device, role="scope-channel")
        device.get_measure_item.assert_called_once_with("FREQuency", 2)
        assert result == {"message": "1000.0 Hz", "value": 1000.0}

    @pytest.mark.parametrize("action", sorted(net_command._SCOPE_MEASUREMENTS))
    def test_every_name_is_a_rigol_measurement_item(self, action):
        from lager.instrument_wrappers.rigol_mso5000_defines import MeasurementItem

        device = mock.MagicMock()
        device.get_measure_item.return_value = 1.0
        _run("rigol_ch2", action, {}, device, role="scope-channel")
        item = device.get_measure_item.call_args.args[0]
        assert item in {member.value for member in MeasurementItem}

    def test_no_valid_value_is_an_answer_rather_than_a_failure(self):
        """The driver answers None for the Rigol's 'no value' reading."""
        device = mock.MagicMock()
        device.get_measure_item.return_value = None
        result = _run("rigol_ch2", "measure_period", {}, device, role="scope-channel")
        assert result == {"message": "period is not present in this capture"}

    def test_a_picoscope_net_keeps_the_daemon_names(self):
        device = mock.MagicMock()
        device.get_measure_item.return_value = 1000.0
        _run("pico1", "measure_freq", {}, device, role="scope-channel")
        device.get_measure_item.assert_called_once_with("frequency")


class TestTheReadbacks:

    def test_status_describes_every_setting_in_one_line(self):
        device = mock.MagicMock()
        device.get_state.return_value = STATE
        result = _run("pico1", "get_state", {}, device)
        message = result["message"]
        assert message.startswith("running, block mode; 1 ms/div")
        assert "trigger auto A rising at 0.5 V" in message
        assert "average of 16 captures" in message
        assert "A on 1 V/div DC 10x" in message
        assert "B off 0.5 V/div AC 1x" in message
        assert result["value"] == STATE

    def test_status_includes_a_holdoff_that_is_set(self):
        device = mock.MagicMock()
        device.get_state.return_value = dict(
            STATE, trigger=dict(STATE["trigger"], holdoff_s=0.002))
        message = _run("pico1", "get_state", {}, device)["message"]
        assert "trigger auto A rising at 0.5 V, holdoff 2 ms;" in message

    def test_the_trigger_readback_includes_a_holdoff_that_is_set(self):
        device = mock.MagicMock()
        device.get_capture_mode.return_value = "auto"
        device.get_trigger_source.return_value = "A"
        device.get_trigger_slope.return_value = "falling"
        device.get_trigger_level.return_value = -0.25
        device.get_trigger_holdoff.return_value = 0.002
        result = _run("pico1", "get_trigger", {}, device)
        assert result["message"] == ("Trigger auto, source A, falling, level -0.25 V, "
                                     "holdoff 2 ms")
        assert result["value"]["holdoff"] == 0.002

    def test_roll_says_whether_it_is_rolling_now(self):
        device = mock.MagicMock()
        device.get_roll.return_value = {"roll": "auto", "rolling": True}
        assert _run("pico1", "get_roll", {}, device)["message"] == "Roll auto (rolling now)"

    def test_display_lists_what_is_on(self):
        device = mock.MagicMock()
        device.get_display.return_value = {
            "persistence": "infinite", "xy": True,
            "zoom": {"factor": 4.0, "center": 1e-3},
            "math": {"expr": "A-B"}, "fft": {"channel": "B", "window": "flattop"},
        }
        message = _run("pico1", "get_display", {}, device)["message"]
        assert message == ("Display: persistence infinite, XY, zoom x4 at 1 ms, "
                           "math A-B, FFT of B (flattop)")

    def test_an_empty_display_is_normal(self):
        device = mock.MagicMock()
        device.get_display.return_value = {}
        assert _run("pico1", "get_display", {}, device)["message"] == "Display: normal"

    def test_the_spectrum_lists_its_peaks(self):
        device = mock.MagicMock()
        device.fft.return_value = {
            "channel": "A", "window": "hann", "resolution_hz": 244.1,
            "peaks": [{"frequency_hz": 10e3, "vrms": 0.707, "dbv": -3.01},
                      {"frequency_hz": 30e3, "vrms": 0.177, "dbv": -15.05}]}
        result = _run("pico1", "fft", {"channel": "A", "peaks": 2}, device)
        assert result["message"] == ("Channel A spectrum (hann window, 244.1 Hz bins): "
                                     "10 kHz -3.0 dBV, 30 kHz -15.1 dBV")
        device.fft.assert_called_once_with(channel="A", window="hann", peaks=2)

    def test_the_spectrum_on_the_instrument_needs_a_channel(self):
        """With no channel the driver fell back to channel A, silently."""
        device = mock.MagicMock()
        with pytest.raises(net_command.WrongNetForAction, match="one channel"):
            _run("pico1", "fft", {}, device)
        assert device.method_calls == []

    def test_the_spectrum_on_a_channel_net_needs_none(self):
        device = mock.MagicMock()
        device.fft.return_value = {"channel": "A", "window": "hann", "peaks": []}
        _run("pico1", "fft", {}, device, role="scope-channel")
        device.fft.assert_called_once_with(channel=None, window="hann", peaks=5)


class TestTheSetters:

    @pytest.mark.parametrize("action,missing", [
        ("set_acquire", "mode"),
        ("set_trigger_holdoff", "seconds"),
        ("set_roll", "mode"),
    ])
    def test_a_missing_value_is_named(self, action, missing):
        with pytest.raises(KeyError, match=missing):
            _run("pico1", action, {})

    def test_acquire_passes_the_count(self):
        device = mock.MagicMock()
        device.set_acquisition.return_value = {"mode": "average", "average_count": 64}
        result = _run("pico1", "set_acquire", {"mode": "average", "count": 64}, device)
        device.set_acquisition.assert_called_once_with("average", 64)
        assert result["message"] == "Acquisition average of 64 captures"

    def test_holdoff_is_echoed_in_scope_units(self):
        device = mock.MagicMock()
        result = _run("pico1", "set_trigger_holdoff", {"seconds": "5e-6"}, device)
        device.set_trigger_holdoff.assert_called_once_with(5e-6)
        assert result["message"] == "Trigger holdoff 5 us"

    def test_display_passes_only_the_settings_given(self):
        device = mock.MagicMock()
        device.set_display.return_value = {"xy": True}
        _run("pico1", "set_display", {"xy": "on"}, device)
        device.set_display.assert_called_once_with(xy="on")

    def test_an_unknown_display_setting_is_refused_by_name(self):
        device = mock.MagicMock()
        with pytest.raises(ValueError, match="brightness"):
            _run("pico1", "set_display", {"brightness": 5}, device)
        device.set_display.assert_not_called()


class TestABadValueIsRefusedByName:
    """These reached `float()` or `int()` unchecked, so a list or an object
    raised TypeError, which the route answers as a 500."""

    @pytest.mark.parametrize("action,key", [
        ("set_scale", "volts_per_div"),
        ("set_timebase", "seconds_per_div"),
        ("set_probe", "ratio"),
        ("set_offset", "offset"),
        ("set_time_offset", "offset"),
        ("set_trigger_holdoff", "seconds"),
        ("trigger_edge", "level"),
    ])
    @pytest.mark.parametrize("bad", [[1], {"v": 1}, "fast", "nan", "inf"])
    def test_a_number(self, action, key, bad):
        device = mock.MagicMock()
        with pytest.raises(ValueError, match=key):
            _run("pico1", action, {key: bad}, device, role="scope-channel")
        assert device.method_calls == []

    @pytest.mark.parametrize("action,key,params", [
        ("set_acquire", "count", {"mode": "average"}),
        ("fft", "peaks", {"channel": "A"}),
    ])
    @pytest.mark.parametrize("bad", [[1], {"v": 1}, "many", float("inf")])
    def test_a_whole_number(self, action, key, params, bad):
        device = mock.MagicMock()
        with pytest.raises(ValueError, match=key):
            _run("pico1", action, dict(params, **{key: bad}), device)
        assert device.method_calls == []

    def test_a_trigger_mode_the_scope_does_not_have(self):
        """Checked first, so the rest of the edge is not half applied."""
        device = mock.MagicMock()
        with pytest.raises(ValueError, match="trigger mode"):
            _run("pico1", "trigger_edge", {"source": "A", "mode": "roll"}, device)
        assert device.method_calls == []

    @pytest.mark.parametrize("bad", [5, "12", [1], [1, None], {"1": 0.0}, ["a", "b"],
                                     [0.0, float("nan")]])
    def test_a_cursor_pair(self, bad):
        device = mock.MagicMock()
        with pytest.raises(ValueError, match="time cursors"):
            _run("pico1", "set_cursor", {"time": bad}, device)
        assert device.method_calls == []

    def test_a_cursor_pair_reaches_the_driver_as_numbers(self):
        device = mock.MagicMock()
        device.set_cursors.return_value = {"time": [0.0, 0.001]}
        _run("pico1", "set_cursor", {"time": ["0", "1e-3"]}, device)
        device.set_cursors.assert_called_once_with(
            time=[0.0, 0.001], volts=None, channel=None)


class TestTheCouplingReadback:

    def test_it_carries_the_value(self):
        """The message alone left a caller parsing a sentence for the mode."""
        device = mock.MagicMock()
        device.get_channel_coupling.return_value = "AC"
        result = _run("pico1", "get_coupling", {}, device, role="scope-channel")
        assert result == {"message": "Coupling AC", "value": "AC"}
