# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP logic analyzer tools (lager.mcp.tools.logic)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from lager import NetType


def _logic_device():
    la = MagicMock()
    la.measurement.frequency.return_value = 1e6
    la.measurement.period.return_value = 1e-6
    la.measurement.duty_cycle_positive.return_value = 0.5
    la.measurement.duty_cycle_negative.return_value = 0.5
    la.measurement.pulse_width_positive.return_value = 1e-7
    la.measurement.pulse_width_negative.return_value = 1e-7
    ts = la.trigger_settings
    ts.set_mode_normal = MagicMock()
    ts.set_mode_auto = MagicMock()
    ts.set_mode_single = MagicMock()
    ts.set_coupling_DC = MagicMock()
    ts.set_coupling_AC = MagicMock()
    ts.set_coupling_low_freq_reject = MagicMock()
    ts.set_coupling_high_freq_reject = MagicMock()
    ts.edge.set_source = MagicMock()
    ts.edge.set_slope_rising = MagicMock()
    ts.edge.set_slope_falling = MagicMock()
    ts.edge.set_slope_both = MagicMock()
    ts.edge.set_level = MagicMock()
    ts.pulse.set_source = MagicMock()
    ts.pulse.set_level = MagicMock()
    ts.pulse.set_trigger_on_pulse_greater_than_width = MagicMock()
    ts.pulse.set_trigger_on_pulse_less_than_width = MagicMock()
    ts.pulse.set_trigger_on_pulse_less_than_greater_than = MagicMock()
    ts.uart.set_source = MagicMock()
    ts.uart.set_level = MagicMock()
    ts.uart.set_uart_params = MagicMock()
    ts.uart.set_trigger_on_start = MagicMock()
    ts.uart.set_trigger_on_frame_error = MagicMock()
    ts.uart.set_trigger_on_check_error = MagicMock()
    ts.uart.set_trigger_on_data = MagicMock()
    ts.i2c.set_source = MagicMock()
    ts.i2c.set_scl_trigger_level = MagicMock()
    ts.i2c.set_sda_trigger_level = MagicMock()
    ts.i2c.set_trigger_on_start = MagicMock()
    ts.i2c.set_trigger_on_restart = MagicMock()
    ts.i2c.set_trigger_on_stop = MagicMock()
    ts.i2c.set_trigger_on_nack = MagicMock()
    ts.i2c.set_trigger_on_address = MagicMock()
    ts.i2c.set_trigger_on_data = MagicMock()
    ts.i2c.set_trigger_on_addr_data = MagicMock()
    ts.spi.set_source = MagicMock()
    ts.spi.set_sck_trigger_level = MagicMock()
    ts.spi.set_mosi_miso_trigger_level = MagicMock()
    ts.spi.set_cs_trigger_level = MagicMock()
    ts.spi.set_clk_edge_positive = MagicMock()
    ts.spi.set_clk_edge_negative = MagicMock()
    ts.spi.set_trigger_on_timeout = MagicMock()
    ts.spi.set_trigger_on_cs_high = MagicMock()
    ts.spi.set_trigger_data = MagicMock()
    la.cursor.set_a = MagicMock()
    la.cursor.set_b = MagicMock()
    la.cursor.move_a = MagicMock()
    la.cursor.move_b = MagicMock()
    la.cursor.hide = MagicMock()
    return la


@pytest.mark.unit
class TestLogicBasicTools:
    """Verify basic logic tools: enable, disable, threshold."""

    @patch("lager.Net.get")
    def test_enable(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_enable

        result = json.loads(logic_enable(net="logic1"))
        mock_get.assert_called_once_with("logic1", type=NetType.Logic)
        la.enable.assert_called_once_with()
        assert result["enabled"] is True

    @patch("lager.Net.get")
    def test_disable(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_disable

        result = json.loads(logic_disable(net="logic1"))
        la.disable.assert_called_once_with()
        assert result["enabled"] is False

    @patch("lager.Net.get")
    def test_threshold(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_threshold

        result = json.loads(logic_threshold(net="logic1", voltage=1.5))
        la.set_signal_threshold.assert_called_once_with(1.5)
        assert result["threshold_v"] == 1.5

    @patch("lager.Net.get")
    def test_logic_enable_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.logic import logic_enable

        with pytest.raises(RuntimeError, match="device not found"):
            logic_enable(net="logic1")


@pytest.mark.unit
class TestLogicCapture:
    """Verify capture control tools: start, single, stop."""

    @patch("lager.Net.get")
    def test_start(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_start

        result = json.loads(logic_start(net="logic1"))
        la.start_capture.assert_called_once_with()
        la.start_single_capture.assert_not_called()
        assert result["single"] is False

    @patch("lager.Net.get")
    def test_start_single(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_start

        result = json.loads(logic_start(net="logic1", single=True))
        la.start_single_capture.assert_called_once_with()
        la.start_capture.assert_not_called()
        assert result["single"] is True

    @patch("lager.Net.get")
    def test_stop(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_stop

        result = json.loads(logic_stop(net="logic1"))
        la.stop_capture.assert_called_once_with()
        assert result["action"] == "stop"


@pytest.mark.unit
class TestLogicMeasurements:
    """Verify measurement tools call the logic measurement API."""

    @patch("lager.Net.get")
    def test_measure_period(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_measure

        result = json.loads(logic_measure(net="logic1", metric="period"))
        la.measurement.period.assert_called_once_with(display=False, measurement_cursor=False)
        assert result["metric"] == "period"
        assert result["value"] == 1e-6

    @patch("lager.Net.get")
    def test_measure_freq(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_measure

        result = json.loads(logic_measure(net="logic1", metric="freq"))
        la.measurement.frequency.assert_called_once_with(display=False, measurement_cursor=False)
        assert result["value"] == 1e6

    @patch("lager.Net.get")
    def test_measure_dc_pos(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_measure

        json.loads(logic_measure(net="logic1", metric="dc_pos"))
        la.measurement.duty_cycle_positive.assert_called_once_with(
            display=False, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_dc_neg(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_measure

        json.loads(logic_measure(net="logic1", metric="dc_neg"))
        la.measurement.duty_cycle_negative.assert_called_once_with(
            display=False, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_pw_pos(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_measure

        json.loads(logic_measure(net="logic1", metric="pw_pos"))
        la.measurement.pulse_width_positive.assert_called_once_with(
            display=False, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_pw_neg(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_measure

        json.loads(logic_measure(net="logic1", metric="pw_neg"))
        la.measurement.pulse_width_negative.assert_called_once_with(
            display=False, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_freq_with_display(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_measure

        json.loads(logic_measure(net="logic1", metric="freq", display=True))
        la.measurement.frequency.assert_called_once_with(display=True, measurement_cursor=False)

    @patch("lager.Net.get")
    def test_measure_period_with_display(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_measure

        json.loads(logic_measure(net="logic1", metric="period", display=True))
        la.measurement.period.assert_called_once_with(display=True, measurement_cursor=False)

    @patch("lager.Net.get")
    def test_measure_dc_pos_with_display(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_measure

        json.loads(logic_measure(net="logic1", metric="dc_pos", display=True))
        la.measurement.duty_cycle_positive.assert_called_once_with(
            display=True, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_unknown_metric(self, mock_get):
        from lager.mcp.tools.logic import logic_measure

        result = json.loads(logic_measure(net="logic1", metric="not_a_metric"))
        mock_get.assert_not_called()
        assert result["status"] == "error"
        assert "Unknown metric" in result["error"]

    @patch("lager.Net.get")
    def test_logic_measure_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.logic import logic_measure

        with pytest.raises(RuntimeError, match="device not found"):
            logic_measure(net="logic1", metric="freq")


@pytest.mark.unit
class TestLogicTriggers:
    """Verify trigger configuration tools."""

    @patch("lager.Net.get")
    def test_trigger_edge_defaults(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_trigger_edge

        json.loads(logic_trigger_edge(net="logic1"))
        la.trigger_settings.set_mode_normal.assert_called_once_with()
        la.trigger_settings.set_coupling_DC.assert_called_once_with()

    @patch("lager.Net.get")
    def test_trigger_edge_with_all_options(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_trigger_edge

        json.loads(logic_trigger_edge(
            net="logic1",
            source="CH1", slope="rising", level=1.5,
            mode="auto", coupling="ac",
        ))
        la.trigger_settings.set_mode_auto.assert_called_once_with()
        la.trigger_settings.set_coupling_AC.assert_called_once_with()
        la.trigger_settings.edge.set_source.assert_called_once_with("CH1")
        la.trigger_settings.edge.set_slope_rising.assert_called_once_with()
        la.trigger_settings.edge.set_level.assert_called_once_with(1.5)

    @patch("lager.Net.get")
    def test_trigger_pulse_defaults(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_trigger_pulse

        json.loads(logic_trigger_pulse(net="logic1"))
        la.trigger_settings.set_mode_normal.assert_called_once_with()
        la.trigger_settings.set_coupling_DC.assert_called_once_with()

    @patch("lager.Net.get")
    def test_trigger_pulse_greater_width(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_trigger_pulse

        json.loads(logic_trigger_pulse(
            net="logic1",
            source="CH1", level=2.0,
            condition="greater", width=1e-6,
        ))
        la.trigger_settings.pulse.set_source.assert_called_once_with("CH1")
        la.trigger_settings.pulse.set_level.assert_called_once_with(2.0)
        la.trigger_settings.pulse.set_trigger_on_pulse_greater_than_width.assert_called_once_with(
            1e-6,
        )

    @patch("lager.Net.get")
    def test_trigger_pulse_range(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_trigger_pulse

        json.loads(logic_trigger_pulse(
            net="logic1",
            condition="range",
            upper=0.001, lower=0.0001,
        ))
        la.trigger_settings.pulse.set_trigger_on_pulse_less_than_greater_than.assert_called_once_with(
            max_pulse_width=0.001, min_pulse_width=0.0001,
        )

    @patch("lager.Net.get")
    def test_trigger_i2c_defaults(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_trigger_i2c

        json.loads(logic_trigger_i2c(net="logic1"))
        la.trigger_settings.set_mode_normal.assert_called_once_with()
        la.trigger_settings.i2c.set_trigger_on_start.assert_called_once_with()

    @patch("lager.Net.get")
    def test_trigger_i2c_address_mode(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_trigger_i2c

        json.loads(logic_trigger_i2c(
            net="logic1",
            trigger_on="address",
            addr_bits=7,
            data_width=2,
            direction="write",
            source_scl="CH1", source_sda="CH2",
            level_scl=1.5, level_sda=1.5,
            address="0x48", data="0xFF",
        ))
        la.trigger_settings.i2c.set_source.assert_called_once_with(net_scl="CH1", net_sda="CH2")
        la.trigger_settings.i2c.set_trigger_on_address.assert_called_once_with(
            bits=7, direction="write", address="0x48",
        )

    @patch("lager.Net.get")
    def test_trigger_uart_defaults(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_trigger_uart

        json.loads(logic_trigger_uart(net="logic1"))
        la.trigger_settings.uart.set_trigger_on_start.assert_called_once_with()
        la.trigger_settings.uart.set_uart_params.assert_called_once_with(
            parity="none", stopbits="1", baud=9600, bits=8,
        )

    @patch("lager.Net.get")
    def test_trigger_uart_data_mode(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_trigger_uart

        json.loads(logic_trigger_uart(
            net="logic1",
            trigger_on="data",
            parity="even", stop_bits="2",
            baud=115200, data_width=8, data="AB",
            source="CH1", level=2.0,
        ))
        la.trigger_settings.uart.set_uart_params.assert_called_once_with(
            parity="even", stopbits="2", baud=115200, bits=8,
        )
        la.trigger_settings.uart.set_trigger_on_data.assert_called_once_with(data="AB")

    @patch("lager.Net.get")
    def test_trigger_spi_defaults(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_trigger_spi

        json.loads(logic_trigger_spi(net="logic1"))
        la.trigger_settings.spi.set_clk_edge_positive.assert_called_once_with()
        la.trigger_settings.spi.set_trigger_on_cs_high.assert_called_once_with()

    @patch("lager.Net.get")
    def test_trigger_spi_with_sources_and_data(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_trigger_spi

        json.loads(logic_trigger_spi(
            net="logic1",
            trigger_on="cs",
            data_bits=8,
            clk_edge="positive",
            data="DE",
            source_mosi_miso="CH1", source_sck="CH2", source_cs="CH3",
            level_mosi_miso=1.5, level_sck=1.5, level_cs=1.5,
        ))
        la.trigger_settings.spi.set_source.assert_called_once_with(
            net_sck="CH2", net_mosi_miso="CH1", net_cs="CH3",
        )
        la.trigger_settings.spi.set_trigger_data.assert_called_once_with(bits=8, data="DE")


@pytest.mark.unit
class TestLogicCursors:
    """Verify cursor control tools."""

    @patch("lager.Net.get")
    def test_cursor_set_a_with_x_and_y(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_cursor_set

        json.loads(logic_cursor_set(net="logic1", cursor="a", x=0.001, y=1.5))
        la.cursor.set_a.assert_called_once_with(x=0.001, y=1.5)

    @patch("lager.Net.get")
    def test_cursor_set_a_no_args(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_cursor_set

        json.loads(logic_cursor_set(net="logic1", cursor="a"))
        la.cursor.set_a.assert_called_once_with(x=None, y=None)

    @patch("lager.Net.get")
    def test_cursor_set_b_x_only(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_cursor_set

        json.loads(logic_cursor_set(net="logic1", cursor="b", x=0.002))
        la.cursor.set_b.assert_called_once_with(x=0.002, y=None)

    @patch("lager.Net.get")
    def test_cursor_move_a(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_cursor_move

        json.loads(logic_cursor_move(net="logic1", cursor="a", x=0.5, y=-0.3))
        la.cursor.move_a.assert_called_once_with(x_del=0.5, y_del=-0.3)

    @patch("lager.Net.get")
    def test_cursor_move_b(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_cursor_move

        json.loads(logic_cursor_move(net="logic1", cursor="b", y=1.0))
        la.cursor.move_b.assert_called_once_with(x_del=None, y_del=1.0)

    @patch("lager.Net.get")
    def test_cursor_hide(self, mock_get):
        la = _logic_device()
        mock_get.return_value = la
        from lager.mcp.tools.logic import logic_cursor_hide

        json.loads(logic_cursor_hide(net="logic1"))
        la.cursor.hide.assert_called_once_with()
