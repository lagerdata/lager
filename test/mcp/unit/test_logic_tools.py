# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP logic analyzer tools (lager.mcp.tools.logic)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestLogicBasicTools:
    """Verify basic logic tools: list_nets, enable, disable."""

    def test_list_nets(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_list_nets
        lager_logic_list_nets(box="X")
        assert_lager_called_with(mock_subprocess, "logic", "--box", "X")

    def test_enable(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_enable
        lager_logic_enable(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess, "logic", "logic1", "enable", "--box", "X",
        )

    def test_disable(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_disable
        lager_logic_disable(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess, "logic", "logic1", "disable", "--box", "X",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_logic_enable_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.logic import lager_logic_enable
        result = lager_logic_enable(box="B", net="logic1")
        assert "Error" in result

    def test_logic_measure_freq_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.logic import lager_logic_measure_freq
        result = lager_logic_measure_freq(box="B", net="logic1")
        assert "Error" in result


@pytest.mark.unit
class TestLogicCapture:
    """Verify capture control tools: start, start_single, stop."""

    def test_start(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_start
        lager_logic_start(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess, "logic", "logic1", "start", "--box", "X",
        )

    def test_start_single(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_start_single
        lager_logic_start_single(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess, "logic", "logic1", "start-single", "--box", "X",
        )

    def test_stop(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_stop
        lager_logic_stop(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess, "logic", "logic1", "stop", "--box", "X",
        )


@pytest.mark.unit
class TestLogicMeasurements:
    """Verify measurement tools build correct commands via _measure helper."""

    def test_measure_period(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_measure_period
        lager_logic_measure_period(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "measure", "period", "--box", "X",
        )

    def test_measure_freq(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_measure_freq
        lager_logic_measure_freq(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "measure", "freq", "--box", "X",
        )

    def test_measure_dc_pos(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_measure_dc_pos
        lager_logic_measure_dc_pos(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "measure", "dc-pos", "--box", "X",
        )

    def test_measure_dc_neg(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_measure_dc_neg
        lager_logic_measure_dc_neg(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "measure", "dc-neg", "--box", "X",
        )

    def test_measure_pw_pos(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_measure_pw_pos
        lager_logic_measure_pw_pos(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "measure", "pw-pos", "--box", "X",
        )

    def test_measure_pw_neg(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_measure_pw_neg
        lager_logic_measure_pw_neg(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "measure", "pw-neg", "--box", "X",
        )

    def test_measure_freq_with_display(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_measure_freq
        lager_logic_measure_freq(box="X", net="logic1", display=True)
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "measure", "freq", "--box", "X", "--display",
        )

    def test_measure_period_with_cursor(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_measure_period
        lager_logic_measure_period(box="X", net="logic1", cursor=True)
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "measure", "period", "--box", "X", "--cursor",
        )

    def test_measure_dc_pos_with_display_and_cursor(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_measure_dc_pos
        lager_logic_measure_dc_pos(
            box="X", net="logic1", display=True, cursor=True,
        )
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "measure", "dc-pos", "--box", "X",
            "--display", "--cursor",
        )


@pytest.mark.unit
class TestLogicTriggers:
    """Verify trigger configuration tools."""

    def test_trigger_edge_defaults(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_trigger_edge
        lager_logic_trigger_edge(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "trigger", "edge", "--box", "X",
            "--mode", "normal", "--coupling", "dc",
        )

    def test_trigger_edge_with_all_options(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_trigger_edge
        lager_logic_trigger_edge(
            box="X", net="logic1",
            source="CH1", slope="rising", level=1.5,
            mode="auto", coupling="ac",
        )
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "trigger", "edge", "--box", "X",
            "--mode", "auto", "--coupling", "ac",
            "--source", "CH1", "--slope", "rising", "--level", "1.5",
        )

    def test_trigger_pulse_defaults(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_trigger_pulse
        lager_logic_trigger_pulse(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "trigger", "pulse", "--box", "X",
            "--mode", "normal", "--coupling", "dc",
        )

    def test_trigger_pulse_with_options(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_trigger_pulse
        lager_logic_trigger_pulse(
            box="X", net="logic1",
            trigger_on="gt", source="CH1", level=2.0,
            upper=0.001, lower=0.0001,
        )
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "trigger", "pulse", "--box", "X",
            "--mode", "normal", "--coupling", "dc",
            "--trigger-on", "gt", "--source", "CH1", "--level", "2.0",
            "--upper", "0.001", "--lower", "0.0001",
        )

    def test_trigger_i2c_defaults(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_trigger_i2c
        lager_logic_trigger_i2c(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "trigger", "i2c", "--box", "X",
            "--mode", "normal", "--coupling", "dc",
        )

    def test_trigger_i2c_with_options(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_trigger_i2c
        lager_logic_trigger_i2c(
            box="X", net="logic1",
            trigger_on="address", addr_width="7",
            data_width="2", direction="write",
            source_scl="CH1", source_sda="CH2",
            level_scl=1.5, level_sda=1.5,
            address=0x48, data=0xFF,
        )
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "trigger", "i2c", "--box", "X",
            "--mode", "normal", "--coupling", "dc",
            "--trigger-on", "address", "--addr-width", "7",
            "--data-width", "2", "--direction", "write",
            "--source-scl", "CH1", "--source-sda", "CH2",
            "--level-scl", "1.5", "--level-sda", "1.5",
            "--address", "72", "--data", "255",
        )

    def test_trigger_uart_defaults(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_trigger_uart
        lager_logic_trigger_uart(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "trigger", "uart", "--box", "X",
            "--mode", "normal", "--coupling", "dc",
        )

    def test_trigger_uart_with_options(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_trigger_uart
        lager_logic_trigger_uart(
            box="X", net="logic1",
            trigger_on="data", parity="even", stop_bits="2",
            baud=115200, data_width=8, data=0xAB,
            source="CH1", level=2.0,
        )
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "trigger", "uart", "--box", "X",
            "--mode", "normal", "--coupling", "dc",
            "--trigger-on", "data", "--parity", "even",
            "--stop-bits", "2", "--baud", "115200",
            "--data-width", "8", "--data", "171",
            "--source", "CH1", "--level", "2.0",
        )

    def test_trigger_spi_defaults(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_trigger_spi
        lager_logic_trigger_spi(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "trigger", "spi", "--box", "X",
            "--mode", "normal", "--coupling", "dc",
        )

    def test_trigger_spi_with_options(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_trigger_spi
        lager_logic_trigger_spi(
            box="X", net="logic1",
            trigger_on="cs", data_width=8,
            clk_slope="positive", cs_idle="high",
            data=0xDE, timeout=0.5,
            source_mosi_miso="CH1", source_sck="CH2", source_cs="CH3",
            level_mosi_miso=1.5, level_sck=1.5, level_cs=1.5,
        )
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "trigger", "spi", "--box", "X",
            "--mode", "normal", "--coupling", "dc",
            "--trigger-on", "cs", "--data-width", "8",
            "--clk-slope", "positive", "--cs-idle", "high",
            "--data", "222", "--timeout", "0.5",
            "--source-mosi-miso", "CH1", "--source-sck", "CH2",
            "--source-cs", "CH3",
            "--level-mosi-miso", "1.5", "--level-sck", "1.5",
            "--level-cs", "1.5",
        )


@pytest.mark.unit
class TestLogicCursors:
    """Verify cursor control tools."""

    def test_cursor_set_a_with_x_and_y(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_cursor_set_a
        lager_logic_cursor_set_a(box="X", net="logic1", x=0.001, y=1.5)
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "cursor", "set-a", "--box", "X",
            "--x", "0.001", "--y", "1.5",
        )

    def test_cursor_set_a_no_args(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_cursor_set_a
        lager_logic_cursor_set_a(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "cursor", "set-a", "--box", "X",
        )

    def test_cursor_set_b_x_only(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_cursor_set_b
        lager_logic_cursor_set_b(box="X", net="logic1", x=0.002)
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "cursor", "set-b", "--box", "X",
            "--x", "0.002",
        )

    def test_cursor_move_a(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_cursor_move_a
        lager_logic_cursor_move_a(box="X", net="logic1", x=0.5, y=-0.3)
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "cursor", "move-a", "--box", "X",
            "--del-x", "0.5", "--del-y", "-0.3",
        )

    def test_cursor_move_b(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_cursor_move_b
        lager_logic_cursor_move_b(box="X", net="logic1", y=1.0)
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "cursor", "move-b", "--box", "X",
            "--del-y", "1.0",
        )

    def test_cursor_hide(self, mock_subprocess):
        from lager.mcp.tools.logic import lager_logic_cursor_hide
        lager_logic_cursor_hide(box="X", net="logic1")
        assert_lager_called_with(
            mock_subprocess,
            "logic", "logic1", "cursor", "hide", "--box", "X",
        )
