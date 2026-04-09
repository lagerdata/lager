# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP oscilloscope tools (lager.mcp.tools.scope)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from lager import NetType


def _scope_device():
    scope = MagicMock()
    scope.measurement.frequency.return_value = 1e6
    scope.measurement.voltage_peak_to_peak.return_value = 3.3
    scope.measurement.voltage_rms.return_value = 1.0
    scope.measurement.voltage_max.return_value = 2.0
    scope.measurement.voltage_min.return_value = -2.0
    scope.measurement.voltage_average.return_value = 0.1
    scope.measurement.period.return_value = 1e-6
    scope.measurement.pulse_width_positive.return_value = 1e-7
    scope.measurement.pulse_width_negative.return_value = 1e-7
    scope.measurement.duty_cycle_positive.return_value = 0.5
    scope.measurement.duty_cycle_negative.return_value = 0.5
    ts = scope.trigger_settings
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
    scope.trace_settings.set_volts_per_div = MagicMock()
    scope.trace_settings.set_time_per_div = MagicMock()
    scope.set_channel_coupling = MagicMock()
    scope.set_channel_probe = MagicMock()
    scope.cursor.set_a = MagicMock()
    scope.cursor.set_b = MagicMock()
    scope.cursor.move_a = MagicMock()
    scope.cursor.move_b = MagicMock()
    scope.cursor.hide = MagicMock()
    scope.cursor.a_x = MagicMock(return_value=1.0)
    scope.cursor.a_y = MagicMock(return_value=2.0)
    scope.cursor.b_x = MagicMock(return_value=3.0)
    scope.cursor.b_y = MagicMock(return_value=4.0)
    scope.cursor.x_delta = MagicMock(return_value=2.0)
    scope.cursor.y_delta = MagicMock(return_value=2.0)
    scope.cursor.frequency = MagicMock(return_value=1e3)
    return scope


@pytest.mark.unit
class TestScopeBasicTools:
    """Verify basic scope tools: autoscale, enable, disable."""

    @patch("lager.Net.get")
    def test_autoscale(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_autoscale

        result = json.loads(scope_autoscale(net="scope1"))
        mock_get.assert_called_once_with("scope1", type=NetType.Analog)
        scope.autoscale.assert_called_once_with()
        assert result["action"] == "autoscale"

    @patch("lager.Net.get")
    def test_enable(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_enable

        result = json.loads(scope_enable(net="scope1"))
        scope.enable.assert_called_once_with()
        assert result["enabled"] is True

    @patch("lager.Net.get")
    def test_disable(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_disable

        result = json.loads(scope_disable(net="scope1"))
        scope.disable.assert_called_once_with()
        assert result["enabled"] is False

    @patch("lager.Net.get")
    def test_scope_enable_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.scope import scope_enable

        with pytest.raises(RuntimeError, match="device not found"):
            scope_enable(net="scope1")

    @patch("lager.Net.get")
    def test_scope_measure_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.scope import scope_measure

        with pytest.raises(RuntimeError, match="device not found"):
            scope_measure(net="scope1", metric="freq")

    @patch("lager.Net.get")
    def test_scope_trigger_edge_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.scope import scope_trigger_edge

        with pytest.raises(RuntimeError, match="device not found"):
            scope_trigger_edge(net="scope1")


@pytest.mark.unit
class TestScopeMeasurements:
    """Verify measurement tools call the scope measurement API."""

    @patch("lager.Net.get")
    def test_measure_freq(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_measure

        result = json.loads(scope_measure(net="scope1", metric="freq"))
        scope.measurement.frequency.assert_called_once_with(display=False, measurement_cursor=False)
        assert result["value"] == 1e6

    @patch("lager.Net.get")
    def test_measure_vpp(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_measure

        json.loads(scope_measure(net="scope1", metric="vpp"))
        scope.measurement.voltage_peak_to_peak.assert_called_once_with(
            display=False, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_vrms(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_measure

        json.loads(scope_measure(net="scope1", metric="vrms"))
        scope.measurement.voltage_rms.assert_called_once_with(
            display=False, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_vmax_with_display(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_measure

        json.loads(scope_measure(net="scope1", metric="vmax", display=True))
        scope.measurement.voltage_max.assert_called_once_with(display=True, measurement_cursor=False)

    @patch("lager.Net.get")
    def test_measure_vmin_with_display(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_measure

        json.loads(scope_measure(net="scope1", metric="vmin", display=True))
        scope.measurement.voltage_min.assert_called_once_with(display=True, measurement_cursor=False)

    @patch("lager.Net.get")
    def test_measure_vavg_with_display(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_measure

        json.loads(scope_measure(net="scope1", metric="vavg", display=True))
        scope.measurement.voltage_average.assert_called_once_with(
            display=True, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_period(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_measure

        json.loads(scope_measure(net="scope1", metric="period"))
        scope.measurement.period.assert_called_once_with(display=False, measurement_cursor=False)

    @patch("lager.Net.get")
    def test_measure_pw_pos(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_measure

        json.loads(scope_measure(net="scope1", metric="pw_pos"))
        scope.measurement.pulse_width_positive.assert_called_once_with(
            display=False, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_pw_neg(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_measure

        json.loads(scope_measure(net="scope1", metric="pw_neg"))
        scope.measurement.pulse_width_negative.assert_called_once_with(
            display=False, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_duty_pos(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_measure

        json.loads(scope_measure(net="scope1", metric="dc_pos"))
        scope.measurement.duty_cycle_positive.assert_called_once_with(
            display=False, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_duty_neg(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_measure

        json.loads(scope_measure(net="scope1", metric="dc_neg"))
        scope.measurement.duty_cycle_negative.assert_called_once_with(
            display=False, measurement_cursor=False,
        )

    @patch("lager.Net.get")
    def test_measure_unknown_metric(self, mock_get):
        from lager.mcp.tools.scope import scope_measure

        result = json.loads(scope_measure(net="scope1", metric="bad"))
        mock_get.assert_not_called()
        assert result["status"] == "error"
        assert "Unknown metric" in result["error"]


@pytest.mark.unit
class TestScopeCapture:
    """Verify capture control tools: start, stop, force."""

    @patch("lager.Net.get")
    def test_start(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_start

        json.loads(scope_start(net="scope1"))
        scope.start_capture.assert_called_once_with()

    @patch("lager.Net.get")
    def test_start_single(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_start

        json.loads(scope_start(net="scope1", single=True))
        scope.start_single_capture.assert_called_once_with()

    @patch("lager.Net.get")
    def test_stop(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_stop

        json.loads(scope_stop(net="scope1"))
        scope.stop_capture.assert_called_once_with()

    @patch("lager.Net.get")
    def test_force(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_force

        json.loads(scope_force(net="scope1"))
        scope.force_trigger.assert_called_once_with()


@pytest.mark.unit
class TestScopeChannelSettings:
    """Verify scope_configure (volts/div, timebase, coupling, probe)."""

    @patch("lager.Net.get")
    def test_volts_per_div(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_configure

        result = json.loads(scope_configure(net="scope1", volts_per_div=0.5))
        scope.trace_settings.set_volts_per_div.assert_called_once_with(0.5)
        assert result["volts_per_div"] == 0.5

    @patch("lager.Net.get")
    def test_time_per_div(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_configure

        result = json.loads(scope_configure(net="scope1", time_per_div=0.001))
        scope.trace_settings.set_time_per_div.assert_called_once_with(0.001)
        assert result["time_per_div"] == 0.001

    @patch("lager.Net.get")
    def test_coupling(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_configure

        result = json.loads(scope_configure(net="scope1", coupling="ac"))
        scope.set_channel_coupling.assert_called_once_with("AC")
        assert result["coupling"] == "ac"

    @patch("lager.Net.get")
    def test_probe(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_configure

        result = json.loads(scope_configure(net="scope1", probe="10x"))
        scope.set_channel_probe.assert_called_once_with(10.0)
        assert result["probe"] == "10x"


@pytest.mark.unit
class TestScopeTriggers:
    """Verify trigger configuration tools."""

    @patch("lager.Net.get")
    def test_trigger_edge_defaults(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_trigger_edge

        json.loads(scope_trigger_edge(net="scope1"))
        scope.trigger_settings.set_mode_normal.assert_called_once_with()

    @patch("lager.Net.get")
    def test_trigger_edge_with_all_options(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_trigger_edge

        json.loads(scope_trigger_edge(
            net="scope1",
            source="CH1", slope="rising", level=1.5,
        ))
        scope.trigger_settings.edge.set_source.assert_called_once_with("CH1")
        scope.trigger_settings.edge.set_slope_rising.assert_called_once_with()
        scope.trigger_settings.edge.set_level.assert_called_once_with(1.5)

    @patch("lager.Net.get")
    def test_trigger_uart_defaults(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_trigger_uart

        json.loads(scope_trigger_uart(net="scope1"))
        scope.trigger_settings.uart.set_trigger_on_start.assert_called_once_with()
        scope.trigger_settings.uart.set_uart_params.assert_called_once_with(
            parity="none", stopbits="1", baud=9600, bits=8,
        )

    @patch("lager.Net.get")
    def test_trigger_uart_with_optional_params(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_trigger_uart

        json.loads(scope_trigger_uart(
            net="scope1",
            baud=115200, source="CH2", level=2.0, data="0xAB",
        ))
        scope.trigger_settings.uart.set_uart_params.assert_called_once_with(
            parity="none", stopbits="1", baud=115200, bits=8,
        )

    @patch("lager.Net.get")
    def test_trigger_i2c_defaults(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_trigger_i2c

        json.loads(scope_trigger_i2c(net="scope1"))
        scope.trigger_settings.i2c.set_trigger_on_start.assert_called_once_with()

    @patch("lager.Net.get")
    def test_trigger_i2c_with_optional_params(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_trigger_i2c

        json.loads(scope_trigger_i2c(
            net="scope1",
            source_scl="CH1", source_sda="CH2",
            level_scl=1.5, level_sda=1.5,
            address="0x48", data="0xFF",
        ))
        scope.trigger_settings.i2c.set_source.assert_called_once_with(net_scl="CH1", net_sda="CH2")

    @patch("lager.Net.get")
    def test_trigger_spi_defaults(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_trigger_spi

        json.loads(scope_trigger_spi(net="scope1"))
        scope.trigger_settings.spi.set_clk_edge_positive.assert_called_once_with()
        scope.trigger_settings.spi.set_trigger_on_cs_high.assert_called_once_with()

    @patch("lager.Net.get")
    def test_trigger_spi_with_optional_params(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_trigger_spi

        json.loads(scope_trigger_spi(
            net="scope1",
            trigger_on="timeout",
            source_mosi_miso="CH1", source_sck="CH2", source_cs="CH3",
            level_mosi_miso=1.5, level_sck=1.5, level_cs=1.5,
            data="0xDE", timeout=0.5,
        ))
        scope.trigger_settings.spi.set_trigger_on_timeout.assert_called_once_with(0.5)
        scope.trigger_settings.spi.set_source.assert_called_once_with(
            net_sck="CH2", net_mosi_miso="CH1", net_cs="CH3",
        )

    @patch("lager.Net.get")
    def test_trigger_spi_cs_mode_with_data(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_trigger_spi

        json.loads(scope_trigger_spi(
            net="scope1",
            trigger_on="cs",
            data="0xDE",
        ))
        scope.trigger_settings.spi.set_trigger_data.assert_called_once_with(bits=8, data="0xDE")

    @patch("lager.Net.get")
    def test_trigger_pulse_defaults(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_trigger_pulse

        json.loads(scope_trigger_pulse(net="scope1"))
        scope.trigger_settings.set_mode_normal.assert_called_once_with()

    @patch("lager.Net.get")
    def test_trigger_pulse_with_optional_params(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_trigger_pulse

        json.loads(scope_trigger_pulse(
            net="scope1",
            source="CH1", level=2.0, upper=0.001, lower=0.0001,
            condition="range",
        ))
        scope.trigger_settings.pulse.set_trigger_on_pulse_less_than_greater_than.assert_called_once_with(
            max_pulse_width=0.001, min_pulse_width=0.0001,
        )


@pytest.mark.unit
class TestScopeCursors:
    """Verify cursor control tools."""

    @patch("lager.Net.get")
    def test_cursor_set_a_with_x_and_y(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_cursor_set

        json.loads(scope_cursor_set(net="scope1", cursor="a", x=0.001, y=1.5))
        scope.cursor.set_a.assert_called_once_with(x=0.001, y=1.5)

    @patch("lager.Net.get")
    def test_cursor_set_b_x_only(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_cursor_set

        json.loads(scope_cursor_set(net="scope1", cursor="b", x=0.002))
        scope.cursor.set_b.assert_called_once_with(x=0.002, y=None)

    @patch("lager.Net.get")
    def test_cursor_move_a(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_cursor_move

        json.loads(scope_cursor_move(net="scope1", cursor="a", x=0.5, y=-0.3))
        scope.cursor.move_a.assert_called_once_with(x_del=0.5, y_del=-0.3)

    @patch("lager.Net.get")
    def test_cursor_move_b(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_cursor_move

        json.loads(scope_cursor_move(net="scope1", cursor="b", y=1.0))
        scope.cursor.move_b.assert_called_once_with(x_del=None, y_del=1.0)

    @patch("lager.Net.get")
    def test_cursor_hide(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_cursor_hide

        json.loads(scope_cursor_hide(net="scope1"))
        scope.cursor.hide.assert_called_once_with()

    @patch("lager.Net.get")
    def test_cursor_read(self, mock_get):
        scope = _scope_device()
        mock_get.return_value = scope
        from lager.mcp.tools.scope import scope_cursor_read

        result = json.loads(scope_cursor_read(net="scope1"))
        assert result["status"] == "ok"
        assert result["a_x"] == 1.0
        assert result["frequency"] == 1e3
