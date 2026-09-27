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
]

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


def _run(netname, action, params=None, device=None):
    device = device if device is not None else mock.MagicMock()
    with mock.patch.object(net_command, "Net") as NetMock, \
            mock.patch.object(net_command, "_proxy", return_value=device):
        NetMock.get_local_nets.return_value = NETS
        return net_command._scope(netname, "scope", action, dict(params or {}))


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
