# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP battery simulator tools (lager.mcp.tools.battery)."""

import json
from unittest.mock import patch, MagicMock

import pytest
from lager import NetType


@pytest.mark.unit
@pytest.mark.battery
class TestBatteryTools:
    """Verify each battery simulator tool calls the correct Net API."""

    # -- soc -------------------------------------------------------------

    @patch("lager.Net.get")
    def test_soc_read(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_soc

        result = json.loads(battery_soc(net="bat1"))
        mock_get.assert_called_once_with("bat1", type=NetType.Battery)
        batt.soc.assert_called_once_with(None)
        assert result["action"] == "read_soc"

    @patch("lager.Net.get")
    def test_soc_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_soc

        result = json.loads(battery_soc(net="bat1", value=80.0))
        batt.soc.assert_called_once_with(80.0)
        assert result["soc"] == 80.0

    # -- voc -------------------------------------------------------------

    @patch("lager.Net.get")
    def test_voc_read(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_voc

        result = json.loads(battery_voc(net="bat1"))
        batt.voc.assert_called_once_with(None)
        assert result["action"] == "read_voc"

    @patch("lager.Net.get")
    def test_voc_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_voc

        result = json.loads(battery_voc(net="bat1", value=3.7))
        batt.voc.assert_called_once_with(3.7)
        assert result["voc"] == 3.7

    # -- current_limit ---------------------------------------------------

    @patch("lager.Net.get")
    def test_current_limit_read(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_current_limit

        result = json.loads(battery_current_limit(net="bat1"))
        batt.current_limit.assert_called_once_with(None)
        assert result["action"] == "read_current_limit"

    @patch("lager.Net.get")
    def test_current_limit_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_current_limit

        result = json.loads(battery_current_limit(net="bat1", value=2.0))
        batt.current_limit.assert_called_once_with(2.0)
        assert result["current_limit"] == 2.0

    # -- mode ------------------------------------------------------------

    @patch("lager.Net.get")
    def test_mode_read(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_mode

        result = json.loads(battery_mode(net="bat1"))
        batt.mode.assert_called_once_with(None)
        assert result["action"] == "read_mode"

    @patch("lager.Net.get")
    def test_mode_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_mode

        result = json.loads(battery_mode(net="bat1", mode_type="dynamic"))
        batt.mode.assert_called_once_with("dynamic")
        assert result["mode"] == "dynamic"

    # -- voltage_full (batt_full) ---------------------------------------

    @patch("lager.Net.get")
    def test_batt_full_read(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_voltage_full

        result = json.loads(battery_voltage_full(net="bat1"))
        batt.voltage_full.assert_called_once_with(None)
        assert result["action"] == "read_voltage_full"

    @patch("lager.Net.get")
    def test_batt_full_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_voltage_full

        result = json.loads(battery_voltage_full(net="bat1", voltage=4.2))
        batt.voltage_full.assert_called_once_with(4.2)
        assert result["voltage_full"] == 4.2

    # -- voltage_empty (batt_empty) --------------------------------------

    @patch("lager.Net.get")
    def test_batt_empty_read(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_voltage_empty

        result = json.loads(battery_voltage_empty(net="bat1"))
        batt.voltage_empty.assert_called_once_with(None)
        assert result["action"] == "read_voltage_empty"

    @patch("lager.Net.get")
    def test_batt_empty_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_voltage_empty

        result = json.loads(battery_voltage_empty(net="bat1", voltage=2.8))
        batt.voltage_empty.assert_called_once_with(2.8)
        assert result["voltage_empty"] == 2.8

    # -- capacity --------------------------------------------------------

    @patch("lager.Net.get")
    def test_capacity_read(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_capacity

        result = json.loads(battery_capacity(net="bat1"))
        batt.capacity.assert_called_once_with(None)
        assert result["action"] == "read_capacity"

    @patch("lager.Net.get")
    def test_capacity_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_capacity

        result = json.loads(battery_capacity(net="bat1", amp_hours=3.5))
        batt.capacity.assert_called_once_with(3.5)
        assert result["capacity_ah"] == 3.5

    # -- ovp -------------------------------------------------------------

    @patch("lager.Net.get")
    def test_ovp_read(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_ovp

        result = json.loads(battery_ovp(net="bat1"))
        batt.ovp.assert_called_once_with(None)
        assert result["action"] == "read_ovp"

    @patch("lager.Net.get")
    def test_ovp_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_ovp

        result = json.loads(battery_ovp(net="bat1", voltage=4.5))
        batt.ovp.assert_called_once_with(4.5)
        assert result["ovp"] == 4.5

    # -- ocp -------------------------------------------------------------

    @patch("lager.Net.get")
    def test_ocp_read(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_ocp

        result = json.loads(battery_ocp(net="bat1"))
        batt.ocp.assert_called_once_with(None)
        assert result["action"] == "read_ocp"

    @patch("lager.Net.get")
    def test_ocp_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_ocp

        result = json.loads(battery_ocp(net="bat1", current=5.0))
        batt.ocp.assert_called_once_with(5.0)
        assert result["ocp"] == 5.0

    # -- model -----------------------------------------------------------

    @patch("lager.Net.get")
    def test_model_read(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_model

        result = json.loads(battery_model(net="bat1"))
        batt.model.assert_called_once_with(None)
        assert result["action"] == "read_model"

    @patch("lager.Net.get")
    def test_model_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_model

        result = json.loads(battery_model(net="bat1", partnumber="NCR18650B"))
        batt.model.assert_called_once_with("NCR18650B")
        assert result["model"] == "NCR18650B"

    # -- action tools (no get/set) ---------------------------------------

    @patch("lager.Net.get")
    def test_enable(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_enable

        result = json.loads(battery_enable(net="bat1"))
        batt.enable.assert_called_once_with()
        assert result["enabled"] is True

    @patch("lager.Net.get")
    def test_disable(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_disable

        result = json.loads(battery_disable(net="bat1"))
        batt.disable.assert_called_once_with()
        assert result["enabled"] is False

    @patch("lager.Net.get")
    def test_state(self, mock_get):
        batt = MagicMock()
        batt.terminal_voltage.return_value = 3.8
        batt.current.return_value = -0.5
        batt.esr.return_value = 0.05
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_state

        result = json.loads(battery_state(net="bat1"))
        assert result["terminal_voltage"] == 3.8
        assert result["current"] == -0.5
        assert result["esr"] == 0.05

    @patch("lager.Net.get")
    def test_clear_ocp(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_clear_ocp

        result = json.loads(battery_clear_ocp(net="bat1"))
        batt.clear_ocp.assert_called_once_with()
        assert result["cleared"] == "ocp"

    @patch("lager.Net.get")
    def test_clear_ovp(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_clear_ovp

        result = json.loads(battery_clear_ovp(net="bat1"))
        batt.clear_ovp.assert_called_once_with()
        assert result["cleared"] == "ovp"

    @patch("lager.Net.get")
    def test_set(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_set

        result = json.loads(battery_set(net="bat1"))
        batt.set_mode_battery.assert_called_once_with()
        assert result["action"] == "set_mode_battery"

    @patch("lager.Net.get")
    def test_clear(self, mock_get):
        batt = MagicMock()
        mock_get.return_value = batt
        from lager.mcp.tools.battery import battery_clear

        result = json.loads(battery_clear(net="bat1"))
        batt.clear_ocp.assert_called_once_with()
        batt.clear_ovp.assert_called_once_with()
        assert result["cleared"] == "all"

    # -- Net.get / device errors -----------------------------------------

    @patch("lager.Net.get")
    def test_soc_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_soc

        with pytest.raises(RuntimeError, match="device not found"):
            battery_soc(net="bat1")

    @patch("lager.Net.get")
    def test_voc_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_voc

        with pytest.raises(RuntimeError, match="device not found"):
            battery_voc(net="bat1")

    @patch("lager.Net.get")
    def test_enable_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_enable

        with pytest.raises(RuntimeError, match="device not found"):
            battery_enable(net="bat1")

    @patch("lager.Net.get")
    def test_disable_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_disable

        with pytest.raises(RuntimeError, match="device not found"):
            battery_disable(net="bat1")

    @patch("lager.Net.get")
    def test_state_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_state

        with pytest.raises(RuntimeError, match="device not found"):
            battery_state(net="bat1")

    @patch("lager.Net.get")
    def test_current_limit_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_current_limit

        with pytest.raises(RuntimeError, match="device not found"):
            battery_current_limit(net="bat1")

    @patch("lager.Net.get")
    def test_mode_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_mode

        with pytest.raises(RuntimeError, match="device not found"):
            battery_mode(net="bat1")

    @patch("lager.Net.get")
    def test_capacity_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_capacity

        with pytest.raises(RuntimeError, match="device not found"):
            battery_capacity(net="bat1")

    @patch("lager.Net.get")
    def test_ovp_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_ovp

        with pytest.raises(RuntimeError, match="device not found"):
            battery_ovp(net="bat1")

    @patch("lager.Net.get")
    def test_ocp_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_ocp

        with pytest.raises(RuntimeError, match="device not found"):
            battery_ocp(net="bat1")

    @patch("lager.Net.get")
    def test_batt_full_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_voltage_full

        with pytest.raises(RuntimeError, match="device not found"):
            battery_voltage_full(net="bat1")

    @patch("lager.Net.get")
    def test_batt_empty_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_voltage_empty

        with pytest.raises(RuntimeError, match="device not found"):
            battery_voltage_empty(net="bat1")

    @patch("lager.Net.get")
    def test_model_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_model

        with pytest.raises(RuntimeError, match="device not found"):
            battery_model(net="bat1")

    @patch("lager.Net.get")
    def test_clear_ocp_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_clear_ocp

        with pytest.raises(RuntimeError, match="device not found"):
            battery_clear_ocp(net="bat1")

    @patch("lager.Net.get")
    def test_clear_ovp_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_clear_ovp

        with pytest.raises(RuntimeError, match="device not found"):
            battery_clear_ovp(net="bat1")

    @patch("lager.Net.get")
    def test_set_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_set

        with pytest.raises(RuntimeError, match="device not found"):
            battery_set(net="bat1")

    @patch("lager.Net.get")
    def test_clear_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.battery import battery_clear

        with pytest.raises(RuntimeError, match="device not found"):
            battery_clear(net="bat1")
