# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP oscilloscope tools (lager.mcp.tools.scope)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestScopeBasicTools:
    """Verify basic scope tools: list_nets, autoscale, enable, disable."""

    def test_list_nets(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_list_nets
        lager_scope_list_nets(box="X")
        assert_lager_called_with(mock_subprocess, "scope", "--box", "X")

    def test_autoscale(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_autoscale
        lager_scope_autoscale(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess, "scope", "scope1", "autoscale", "--box", "X",
        )

    def test_enable(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_enable
        lager_scope_enable(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess, "scope", "scope1", "enable", "--box", "X",
        )

    def test_disable(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_disable
        lager_scope_disable(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess, "scope", "scope1", "disable", "--box", "X",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_scope_enable_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.scope import lager_scope_enable
        result = lager_scope_enable(box="B", net="scope1")
        assert "Error" in result

    def test_scope_measure_freq_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.scope import lager_scope_measure_freq
        result = lager_scope_measure_freq(box="B", net="scope1")
        assert "Error" in result

    def test_scope_trigger_edge_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.scope import lager_scope_trigger_edge
        result = lager_scope_trigger_edge(box="B", net="scope1")
        assert "Error" in result

    def test_scope_stream_start_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.scope import lager_scope_stream_start
        result = lager_scope_stream_start(box="B", net="scope1")
        assert "Error" in result


@pytest.mark.unit
class TestScopeMeasurements:
    """Verify measurement tools build correct commands via _measure helper."""

    def test_measure_freq(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_measure_freq
        lager_scope_measure_freq(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "measure", "freq", "--box", "X",
        )

    def test_measure_vpp(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_measure_vpp
        lager_scope_measure_vpp(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "measure", "vpp", "--box", "X",
        )

    def test_measure_vrms(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_measure_vrms
        lager_scope_measure_vrms(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "measure", "vrms", "--box", "X",
        )

    def test_measure_vmax_with_display(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_measure_vmax
        lager_scope_measure_vmax(box="X", net="scope1", display=True)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "measure", "vmax", "--box", "X", "--display",
        )

    def test_measure_vmin_with_cursor(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_measure_vmin
        lager_scope_measure_vmin(box="X", net="scope1", cursor=True)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "measure", "vmin", "--box", "X", "--cursor",
        )

    def test_measure_vavg_with_display_and_cursor(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_measure_vavg
        lager_scope_measure_vavg(box="X", net="scope1", display=True, cursor=True)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "measure", "vavg", "--box", "X",
            "--display", "--cursor",
        )

    def test_measure_period(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_measure_period
        lager_scope_measure_period(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "measure", "period", "--box", "X",
        )

    def test_measure_pw_pos(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_measure_pw_pos
        lager_scope_measure_pw_pos(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "measure", "pulse-width-pos", "--box", "X",
        )

    def test_measure_pw_neg(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_measure_pw_neg
        lager_scope_measure_pw_neg(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "measure", "pulse-width-neg", "--box", "X",
        )

    def test_measure_duty_pos(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_measure_duty_pos
        lager_scope_measure_duty_pos(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "measure", "duty-cycle-pos", "--box", "X",
        )

    def test_measure_duty_neg(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_measure_duty_neg
        lager_scope_measure_duty_neg(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "measure", "duty-cycle-neg", "--box", "X",
        )


@pytest.mark.unit
class TestScopeCapture:
    """Verify capture control tools: start, stop, force."""

    def test_start(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_start
        lager_scope_start(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess, "scope", "scope1", "start", "--box", "X",
        )

    def test_start_single(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_start
        lager_scope_start(box="X", net="scope1", single=True)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "start", "--single", "--box", "X",
        )

    def test_stop(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_stop
        lager_scope_stop(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess, "scope", "scope1", "stop", "--box", "X",
        )

    def test_force(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_force
        lager_scope_force(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess, "scope", "scope1", "force", "--box", "X",
        )


@pytest.mark.unit
class TestScopeChannelSettings:
    """Verify channel setting tools: scale, coupling, probe, timebase."""

    def test_scale(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_scale
        lager_scope_scale(box="X", net="scope1", volts_per_div=0.5)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "scale", "0.5", "--box", "X",
        )

    def test_coupling(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_coupling
        lager_scope_coupling(box="X", net="scope1", mode="ac")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "coupling", "ac", "--box", "X",
        )

    def test_probe(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_probe
        lager_scope_probe(box="X", net="scope1", attenuation="10x")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "probe", "10x", "--box", "X",
        )

    def test_timebase(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_timebase
        lager_scope_timebase(box="X", net="scope1", seconds_per_div=0.001)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "timebase", "0.001", "--box", "X",
        )


@pytest.mark.unit
class TestScopeTriggers:
    """Verify trigger configuration tools."""

    def test_trigger_edge_defaults(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_trigger_edge
        lager_scope_trigger_edge(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "trigger", "edge", "--box", "X",
        )

    def test_trigger_edge_with_all_options(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_trigger_edge
        lager_scope_trigger_edge(
            box="X", net="scope1",
            source="CH1", slope="rising", level=1.5,
        )
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "trigger", "edge", "--box", "X",
            "--source", "CH1", "--slope", "rising", "--level", "1.5",
        )

    def test_trigger_uart_defaults(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_trigger_uart
        lager_scope_trigger_uart(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "trigger", "uart", "--box", "X",
            "--baud", "9600", "--parity", "none",
            "--stop-bits", "1", "--data-width", "8",
            "--trigger-on", "start", "--mode", "normal", "--coupling", "dc",
        )

    def test_trigger_uart_with_optional_params(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_trigger_uart
        lager_scope_trigger_uart(
            box="X", net="scope1",
            baud=115200, source="CH2", level=2.0, data="0xAB",
        )
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "trigger", "uart", "--box", "X",
            "--baud", "115200", "--parity", "none",
            "--stop-bits", "1", "--data-width", "8",
            "--trigger-on", "start", "--mode", "normal", "--coupling", "dc",
            "--source", "CH2", "--level", "2.0", "--data", "0xAB",
        )

    def test_trigger_i2c_defaults(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_trigger_i2c
        lager_scope_trigger_i2c(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "trigger", "i2c", "--box", "X",
            "--trigger-on", "start", "--addr-width", "7",
            "--data-width", "8", "--direction", "read_write",
            "--mode", "normal", "--coupling", "dc",
        )

    def test_trigger_i2c_with_optional_params(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_trigger_i2c
        lager_scope_trigger_i2c(
            box="X", net="scope1",
            source_scl="CH1", source_sda="CH2",
            level_scl=1.5, level_sda=1.5,
            address="0x48", data="0xFF",
        )
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "trigger", "i2c", "--box", "X",
            "--trigger-on", "start", "--addr-width", "7",
            "--data-width", "8", "--direction", "read_write",
            "--mode", "normal", "--coupling", "dc",
            "--source-scl", "CH1", "--source-sda", "CH2",
            "--level-scl", "1.5", "--level-sda", "1.5",
            "--address", "0x48", "--data", "0xFF",
        )

    def test_trigger_spi_defaults(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_trigger_spi
        lager_scope_trigger_spi(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "trigger", "spi", "--box", "X",
            "--trigger-on", "cs", "--data-width", "8",
            "--clk-slope", "rising", "--cs-idle", "high",
            "--mode", "normal", "--coupling", "dc",
        )

    def test_trigger_spi_with_optional_params(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_trigger_spi
        lager_scope_trigger_spi(
            box="X", net="scope1",
            source_mosi_miso="CH1", source_sck="CH2", source_cs="CH3",
            level_mosi_miso=1.5, level_sck=1.5, level_cs=1.5,
            data="0xDE", timeout=0.5,
        )
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "trigger", "spi", "--box", "X",
            "--trigger-on", "cs", "--data-width", "8",
            "--clk-slope", "rising", "--cs-idle", "high",
            "--mode", "normal", "--coupling", "dc",
            "--source-mosi-miso", "CH1", "--source-sck", "CH2",
            "--source-cs", "CH3",
            "--level-mosi-miso", "1.5", "--level-sck", "1.5",
            "--level-cs", "1.5",
            "--data", "0xDE", "--timeout", "0.5",
        )

    def test_trigger_pulse_defaults(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_trigger_pulse
        lager_scope_trigger_pulse(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "trigger", "pulse", "--box", "X",
            "--trigger-on", "positive", "--mode", "normal", "--coupling", "dc",
        )

    def test_trigger_pulse_with_optional_params(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_trigger_pulse
        lager_scope_trigger_pulse(
            box="X", net="scope1",
            source="CH1", level=2.0, upper=0.001, lower=0.0001,
        )
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "trigger", "pulse", "--box", "X",
            "--trigger-on", "positive", "--mode", "normal", "--coupling", "dc",
            "--source", "CH1", "--level", "2.0",
            "--upper", "0.001", "--lower", "0.0001",
        )


@pytest.mark.unit
class TestScopeCursors:
    """Verify cursor control tools."""

    def test_cursor_set_a_with_x_and_y(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_cursor_set_a
        lager_scope_cursor_set_a(box="X", net="scope1", x=0.001, y=1.5)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "cursor", "set-a", "--box", "X",
            "--x", "0.001", "--y", "1.5",
        )

    def test_cursor_set_b_x_only(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_cursor_set_b
        lager_scope_cursor_set_b(box="X", net="scope1", x=0.002)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "cursor", "set-b", "--box", "X",
            "--x", "0.002",
        )

    def test_cursor_move_a(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_cursor_move_a
        lager_scope_cursor_move_a(box="X", net="scope1", x=0.5, y=-0.3)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "cursor", "move-a", "--box", "X",
            "--x", "0.5", "--y", "-0.3",
        )

    def test_cursor_move_b(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_cursor_move_b
        lager_scope_cursor_move_b(box="X", net="scope1", y=1.0)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "cursor", "move-b", "--box", "X",
            "--y", "1.0",
        )

    def test_cursor_hide(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_cursor_hide
        lager_scope_cursor_hide(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "cursor", "hide", "--box", "X",
        )


@pytest.mark.unit
class TestScopeStreaming:
    """Verify PicoScope streaming tools."""

    def test_stream_start_defaults(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_stream_start
        lager_scope_stream_start(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "stream", "start", "--box", "X",
            "--channel", "A",
            "--volts-per-div", "1.0",
            "--time-per-div", "0.001",
            "--trigger-level", "0.0",
            "--trigger-slope", "rising",
            "--capture-mode", "auto",
            "--coupling", "dc",
        )

    def test_stream_start_custom(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_stream_start
        lager_scope_stream_start(
            box="X", net="scope1",
            channel="B", volts_per_div=2.0, time_per_div=0.01,
            trigger_level=1.5, trigger_slope="falling",
            capture_mode="normal", coupling="ac",
        )
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "stream", "start", "--box", "X",
            "--channel", "B",
            "--volts-per-div", "2.0",
            "--time-per-div", "0.01",
            "--trigger-level", "1.5",
            "--trigger-slope", "falling",
            "--capture-mode", "normal",
            "--coupling", "ac",
        )

    def test_stream_stop(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_stream_stop
        lager_scope_stream_stop(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "stream", "stop", "--box", "X",
        )

    def test_stream_status(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_stream_status
        lager_scope_stream_status(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "stream", "status", "--box", "X",
        )

    def test_stream_capture_defaults(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_stream_capture
        lager_scope_stream_capture(box="X", net="scope1")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "stream", "capture", "--box", "X",
            "--output", "scope_data.csv", "--duration", "1.0",
        )

    def test_stream_capture_with_samples(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_stream_capture
        lager_scope_stream_capture(
            box="X", net="scope1",
            output="data.csv", duration=2.0, samples=5000,
        )
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "stream", "capture", "--box", "X",
            "--output", "data.csv", "--duration", "2.0",
            "--samples", "5000",
        )

    def test_stream_config_enable_true(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_stream_config
        lager_scope_stream_config(box="X", net="scope1", enable=True)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "stream", "config", "--box", "X",
            "--enable",
        )

    def test_stream_config_enable_false(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_stream_config
        lager_scope_stream_config(box="X", net="scope1", enable=False)
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "stream", "config", "--box", "X",
            "--disable",
        )

    def test_stream_config_enable_none(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_stream_config
        lager_scope_stream_config(box="X", net="scope1", channel="B")
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "stream", "config", "--box", "X",
            "--channel", "B",
        )

    def test_stream_config_multiple_params(self, mock_subprocess):
        from lager.mcp.tools.scope import lager_scope_stream_config
        lager_scope_stream_config(
            box="X", net="scope1",
            channel="A", volts_per_div=0.5, coupling="ac",
            trigger_slope="falling", capture_mode="single",
        )
        assert_lager_called_with(
            mock_subprocess,
            "scope", "scope1", "stream", "config", "--box", "X",
            "--channel", "A",
            "--volts-per-div", "0.5",
            "--trigger-slope", "falling",
            "--capture-mode", "single",
            "--coupling", "ac",
        )
