# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP power supply tools (lager.mcp.tools.power)."""

import json
from unittest.mock import patch, MagicMock

import pytest
from lager import NetType


@pytest.mark.unit
@pytest.mark.power
class TestPowerTools:
    """Verify each power supply tool calls the correct Net API."""

    # -- voltage ---------------------------------------------------------

    @patch("lager.Net.get")
    def test_voltage_read(self, mock_get):
        supply = MagicMock()
        supply.voltage.return_value = 3.3
        mock_get.return_value = supply
        from lager.mcp.tools.power import supply_measure

        result = json.loads(supply_measure(net="psu1", measurement="voltage"))
        mock_get.assert_called_once_with("psu1", type=NetType.PowerSupply)
        supply.voltage.assert_called_once_with()
        assert result["status"] == "ok"
        assert result["value"] == 3.3

    @patch("lager.Net.get")
    def test_voltage_set(self, mock_get):
        supply = MagicMock()
        mock_get.return_value = supply
        from lager.mcp.tools.power import supply_set_voltage

        result = json.loads(supply_set_voltage(net="psu1", voltage=3.3))
        mock_get.assert_called_once_with("psu1", type=NetType.PowerSupply)
        supply.set_voltage.assert_called_once_with(3.3)
        assert result["status"] == "ok"
        assert result["voltage"] == 3.3

    @patch("lager.Net.get")
    def test_voltage_set_with_ocp_and_ovp(self, mock_get):
        supply = MagicMock()
        mock_get.return_value = supply
        from lager.mcp.tools.power import supply_set_voltage, supply_ocp, supply_ovp

        json.loads(supply_set_voltage(net="psu1", voltage=5.0))
        json.loads(supply_ocp(net="psu1", value=1.5))
        json.loads(supply_ovp(net="psu1", value=6.0))
        assert mock_get.call_count == 3
        supply.set_voltage.assert_called_once_with(5.0)
        supply.ocp.assert_called_once_with(1.5)
        supply.ovp.assert_called_once_with(6.0)

    # -- current ---------------------------------------------------------

    @patch("lager.Net.get")
    def test_current_read(self, mock_get):
        supply = MagicMock()
        supply.current.return_value = 0.42
        mock_get.return_value = supply
        from lager.mcp.tools.power import supply_measure

        result = json.loads(supply_measure(net="psu1", measurement="current"))
        supply.current.assert_called_once_with()
        assert result["value"] == 0.42

    @patch("lager.Net.get")
    def test_current_set(self, mock_get):
        supply = MagicMock()
        mock_get.return_value = supply
        from lager.mcp.tools.power import supply_set_current

        result = json.loads(supply_set_current(net="psu1", current=0.5))
        supply.set_current.assert_called_once_with(0.5)
        assert result["current"] == 0.5

    # -- enable / disable ------------------------------------------------

    @patch("lager.Net.get")
    def test_enable(self, mock_get):
        supply = MagicMock()
        mock_get.return_value = supply
        from lager.mcp.tools.power import supply_enable

        result = json.loads(supply_enable(net="psu1"))
        supply.enable.assert_called_once_with()
        assert result["enabled"] is True

    @patch("lager.Net.get")
    def test_disable(self, mock_get):
        supply = MagicMock()
        mock_get.return_value = supply
        from lager.mcp.tools.power import supply_disable

        result = json.loads(supply_disable(net="psu1"))
        supply.disable.assert_called_once_with()
        assert result["enabled"] is False

    # -- state -----------------------------------------------------------

    @patch("lager.Net.get")
    def test_state(self, mock_get):
        supply = MagicMock()
        supply.voltage.return_value = 5.0
        supply.current.return_value = 0.1
        supply.power.return_value = 0.5
        supply.output_is_enabled.return_value = True
        mock_get.return_value = supply
        from lager.mcp.tools.power import supply_state

        result = json.loads(supply_state(net="psu1"))
        assert result["status"] == "ok"
        assert result["voltage"] == 5.0
        assert result["current"] == 0.1
        assert result["power"] == 0.5
        assert result["enabled"] is True

    # -- clear faults ----------------------------------------------------

    @patch("lager.Net.get")
    def test_clear_ocp(self, mock_get):
        supply = MagicMock()
        mock_get.return_value = supply
        from lager.mcp.tools.power import supply_clear_ocp

        result = json.loads(supply_clear_ocp(net="psu1"))
        supply.clear_ocp.assert_called_once_with()
        assert result["cleared"] == "ocp"

    @patch("lager.Net.get")
    def test_clear_ovp(self, mock_get):
        supply = MagicMock()
        mock_get.return_value = supply
        from lager.mcp.tools.power import supply_clear_ovp

        result = json.loads(supply_clear_ovp(net="psu1"))
        supply.clear_ovp.assert_called_once_with()
        assert result["cleared"] == "ovp"

    # -- set mode (apply supply mode) ------------------------------------

    @patch("lager.Net.get")
    def test_set_mode(self, mock_get):
        supply = MagicMock()
        mock_get.return_value = supply
        from lager.mcp.tools.power import supply_set_mode

        result = json.loads(supply_set_mode(net="psu1"))
        supply.set_mode.assert_called_once_with()
        assert result["action"] == "set_mode"

    # -- Net.get / device errors -----------------------------------------

    @patch("lager.Net.get")
    def test_voltage_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.power import supply_measure

        with pytest.raises(RuntimeError, match="device not found"):
            supply_measure(net="psu1", measurement="voltage")

    @patch("lager.Net.get")
    def test_current_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.power import supply_measure

        with pytest.raises(RuntimeError, match="device not found"):
            supply_measure(net="psu1", measurement="current")

    @patch("lager.Net.get")
    def test_enable_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.power import supply_enable

        with pytest.raises(RuntimeError, match="device not found"):
            supply_enable(net="psu1")

    @patch("lager.Net.get")
    def test_disable_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.power import supply_disable

        with pytest.raises(RuntimeError, match="device not found"):
            supply_disable(net="psu1")

    @patch("lager.Net.get")
    def test_state_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.power import supply_state

        with pytest.raises(RuntimeError, match="device not found"):
            supply_state(net="psu1")

    @patch("lager.Net.get")
    def test_clear_ocp_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.power import supply_clear_ocp

        with pytest.raises(RuntimeError, match="device not found"):
            supply_clear_ocp(net="psu1")

    @patch("lager.Net.get")
    def test_clear_ovp_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.power import supply_clear_ovp

        with pytest.raises(RuntimeError, match="device not found"):
            supply_clear_ovp(net="psu1")

    @patch("lager.Net.get")
    def test_set_mode_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.power import supply_set_mode

        with pytest.raises(RuntimeError, match="device not found"):
            supply_set_mode(net="psu1")
