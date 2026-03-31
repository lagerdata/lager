# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP measurement tools (lager.mcp.tools.measurement)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from lager import NetType


@pytest.mark.unit
@pytest.mark.measurement
class TestMeasurementTools:
    """Verify each measurement tool calls the correct Net API."""

    # -- ADC -------------------------------------------------------------

    @patch("lager.Net.get")
    def test_adc_read(self, mock_get):
        device = MagicMock()
        device.input.return_value = 1.25
        mock_get.return_value = device
        from lager.mcp.tools.measurement import adc_read

        result = json.loads(adc_read(net="adc1"))
        mock_get.assert_called_once_with("adc1", type=NetType.ADC)
        device.input.assert_called_once_with()
        assert result["status"] == "ok"
        assert result["net"] == "adc1"
        assert result["voltage"] == 1.25

    # -- DAC -------------------------------------------------------------

    @patch("lager.Net.get")
    def test_dac_read(self, mock_get):
        device = MagicMock()
        device.get_voltage.return_value = 2.5
        mock_get.return_value = device
        from lager.mcp.tools.measurement import dac_read

        result = json.loads(dac_read(net="dac1"))
        mock_get.assert_called_once_with("dac1", type=NetType.DAC)
        device.get_voltage.assert_called_once_with()
        assert result["voltage"] == 2.5

    @patch("lager.Net.get")
    def test_dac_set_voltage(self, mock_get):
        device = MagicMock()
        mock_get.return_value = device
        from lager.mcp.tools.measurement import dac_set

        result = json.loads(dac_set(net="dac1", voltage=2.5))
        mock_get.assert_called_once_with("dac1", type=NetType.DAC)
        device.output.assert_called_once_with(2.5)
        assert result["voltage"] == 2.5

    # -- GPIO ------------------------------------------------------------

    @patch("lager.Net.get")
    def test_gpio_read(self, mock_get):
        device = MagicMock()
        device.input.return_value = 1
        mock_get.return_value = device
        from lager.mcp.tools.measurement import gpio_read

        result = json.loads(gpio_read(net="gpio1"))
        mock_get.assert_called_once_with("gpio1", type=NetType.GPIO)
        device.input.assert_called_once_with()
        assert result["value"] == 1

    @patch("lager.Net.get")
    def test_gpio_set_high(self, mock_get):
        device = MagicMock()
        mock_get.return_value = device
        from lager.mcp.tools.measurement import gpio_set

        result = json.loads(gpio_set(net="gpio1", level=1))
        device.output.assert_called_once_with(1)
        assert result["level"] == 1

    @patch("lager.Net.get")
    def test_gpio_set_low(self, mock_get):
        device = MagicMock()
        mock_get.return_value = device
        from lager.mcp.tools.measurement import gpio_set

        result = json.loads(gpio_set(net="gpio1", level=0))
        device.output.assert_called_once_with(0)
        assert result["level"] == 0

    # -- Thermocouple ----------------------------------------------------

    @patch("lager.Net.get")
    def test_thermocouple_read(self, mock_get):
        device = MagicMock()
        device.read.return_value = 42.5
        mock_get.return_value = device
        from lager.mcp.tools.measurement import thermocouple_read

        result = json.loads(thermocouple_read(net="tc1"))
        mock_get.assert_called_once_with("tc1", type=NetType.Thermocouple)
        device.read.assert_called_once_with()
        assert result["temperature_c"] == 42.5

    # -- Watt meter ------------------------------------------------------

    @patch("lager.Net.get")
    def test_watt_read(self, mock_get):
        device = MagicMock()
        device.read.return_value = 12.3
        mock_get.return_value = device
        from lager.mcp.tools.measurement import watt_read

        result = json.loads(watt_read(net="watt1"))
        mock_get.assert_called_once_with("watt1", type=NetType.WattMeter)
        device.read.assert_called_once_with()
        assert result["power_w"] == 12.3

    @patch("lager.Net.get")
    def test_watt_read_all(self, mock_get):
        device = MagicMock()
        device.read_all.return_value = {"current_a": 1.0, "voltage_v": 5.0, "power_w": 5.0}
        mock_get.return_value = device
        from lager.mcp.tools.measurement import watt_read_all

        result = json.loads(watt_read_all(net="watt1"))
        device.read_all.assert_called_once_with()
        assert result["current_a"] == 1.0
        assert result["power_w"] == 5.0

    @patch("lager.Net.get")
    def test_watt_read_all_fallback_to_read(self, mock_get):
        device = MagicMock()
        device.read_all.side_effect = AttributeError("no read_all")
        device.read.return_value = 9.9
        mock_get.return_value = device
        from lager.mcp.tools.measurement import watt_read_all

        result = json.loads(watt_read_all(net="watt1"))
        device.read.assert_called_once_with()
        assert result["power_w"] == 9.9

    # -- GPIO wait-for ---------------------------------------------------

    @patch("lager.Net.get")
    def test_gpio_wait_for_default_timeout(self, mock_get):
        gpio = MagicMock()
        gpio.wait_for_level.return_value = 0.01
        mock_get.return_value = gpio
        from lager.mcp.tools.measurement import gpio_wait_for

        result = json.loads(gpio_wait_for(net="gpio1", level=1))
        gpio.wait_for_level.assert_called_once_with(1, timeout=30.0)
        assert result["status"] == "ok"
        assert result["level"] == 1

    @patch("lager.Net.get")
    def test_gpio_wait_for_custom_timeout(self, mock_get):
        gpio = MagicMock()
        gpio.wait_for_level.return_value = 0.02
        mock_get.return_value = gpio
        from lager.mcp.tools.measurement import gpio_wait_for

        result = json.loads(gpio_wait_for(net="gpio1", level=0, timeout=5.0))
        gpio.wait_for_level.assert_called_once_with(0, timeout=5.0)
        assert result["elapsed_s"] == 0.02

    @patch("lager.Net.get")
    def test_gpio_wait_for_timeout_error(self, mock_get):
        gpio = MagicMock()
        gpio.wait_for_level.side_effect = TimeoutError()
        mock_get.return_value = gpio
        from lager.mcp.tools.measurement import gpio_wait_for

        result = json.loads(gpio_wait_for(net="gpio1", level=1, timeout=2.0))
        assert result["status"] == "timeout"
        assert result["timeout_s"] == 2.0

    # -- Net.get / device errors -----------------------------------------

    @patch("lager.Net.get")
    def test_adc_read_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.measurement import adc_read

        with pytest.raises(RuntimeError, match="device not found"):
            adc_read(net="adc1")

    @patch("lager.Net.get")
    def test_dac_set_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.measurement import dac_set

        with pytest.raises(RuntimeError, match="device not found"):
            dac_set(net="dac1", voltage=2.5)

    @patch("lager.Net.get")
    def test_gpio_read_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.measurement import gpio_read

        with pytest.raises(RuntimeError, match="device not found"):
            gpio_read(net="gpio1")

    @patch("lager.Net.get")
    def test_gpio_set_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.measurement import gpio_set

        with pytest.raises(RuntimeError, match="device not found"):
            gpio_set(net="gpio1", level=1)

    @patch("lager.Net.get")
    def test_thermocouple_read_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.measurement import thermocouple_read

        with pytest.raises(RuntimeError, match="device not found"):
            thermocouple_read(net="tc1")

    @patch("lager.Net.get")
    def test_watt_read_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.measurement import watt_read

        with pytest.raises(RuntimeError, match="device not found"):
            watt_read(net="watt1")
