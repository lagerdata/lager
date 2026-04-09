# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP electronic load tools (lager.mcp.tools.eload)."""

import json
from unittest.mock import patch, MagicMock

import pytest
from lager import NetType


@pytest.mark.unit
@pytest.mark.eload
class TestEloadTools:
    """Verify each electronic load tool calls the correct Net API."""

    # -- cc (constant current) -------------------------------------------

    @patch("lager.Net.get")
    def test_cc_read(self, mock_get):
        eload = MagicMock()
        eload.current.return_value = 1.5
        mock_get.return_value = eload
        from lager.mcp.tools.eload import eload_set

        result = json.loads(eload_set(net="eload1", mode="cc"))
        mock_get.assert_called_once_with("eload1", type=NetType.ELoad)
        eload.current.assert_called_once_with(None)
        assert result["status"] == "ok"
        assert result["mode"] == "cc"
        assert result["current"] == 1.5

    @patch("lager.Net.get")
    def test_cc_set(self, mock_get):
        eload = MagicMock()
        mock_get.return_value = eload
        from lager.mcp.tools.eload import eload_set

        result = json.loads(eload_set(net="eload1", mode="cc", value=1.5))
        eload.current.assert_called_once_with(1.5)
        assert result["current"] == 1.5

    # -- cv (constant voltage) -------------------------------------------

    @patch("lager.Net.get")
    def test_cv_read(self, mock_get):
        eload = MagicMock()
        eload.voltage.return_value = 12.0
        mock_get.return_value = eload
        from lager.mcp.tools.eload import eload_set

        result = json.loads(eload_set(net="eload1", mode="cv"))
        eload.voltage.assert_called_once_with(None)
        assert result["voltage"] == 12.0

    @patch("lager.Net.get")
    def test_cv_set(self, mock_get):
        eload = MagicMock()
        mock_get.return_value = eload
        from lager.mcp.tools.eload import eload_set

        result = json.loads(eload_set(net="eload1", mode="cv", value=12.0))
        eload.voltage.assert_called_once_with(12.0)
        assert result["voltage"] == 12.0

    # -- cr (constant resistance) ----------------------------------------

    @patch("lager.Net.get")
    def test_cr_read(self, mock_get):
        eload = MagicMock()
        eload.resistance.return_value = 100.0
        mock_get.return_value = eload
        from lager.mcp.tools.eload import eload_set

        result = json.loads(eload_set(net="eload1", mode="cr"))
        eload.resistance.assert_called_once_with(None)
        assert result["resistance"] == 100.0

    @patch("lager.Net.get")
    def test_cr_set(self, mock_get):
        eload = MagicMock()
        mock_get.return_value = eload
        from lager.mcp.tools.eload import eload_set

        result = json.loads(eload_set(net="eload1", mode="cr", value=100.0))
        eload.resistance.assert_called_once_with(100.0)
        assert result["resistance"] == 100.0

    # -- cp (constant power) ---------------------------------------------

    @patch("lager.Net.get")
    def test_cp_read(self, mock_get):
        eload = MagicMock()
        eload.power.return_value = 25.0
        mock_get.return_value = eload
        from lager.mcp.tools.eload import eload_set

        result = json.loads(eload_set(net="eload1", mode="cp"))
        eload.power.assert_called_once_with(None)
        assert result["power"] == 25.0

    @patch("lager.Net.get")
    def test_cp_set(self, mock_get):
        eload = MagicMock()
        mock_get.return_value = eload
        from lager.mcp.tools.eload import eload_set

        result = json.loads(eload_set(net="eload1", mode="cp", value=25.0))
        eload.power.assert_called_once_with(25.0)
        assert result["power"] == 25.0

    # -- state -----------------------------------------------------------

    @patch("lager.Net.get")
    def test_state(self, mock_get):
        eload = MagicMock()
        eload.measured_voltage.return_value = 5.0
        eload.measured_current.return_value = 0.2
        eload.measured_power.return_value = 1.0
        eload.mode.return_value = "cc"
        mock_get.return_value = eload
        from lager.mcp.tools.eload import eload_state

        result = json.loads(eload_state(net="eload1"))
        assert result["status"] == "ok"
        assert result["measured_voltage"] == 5.0
        assert result["measured_current"] == 0.2
        assert result["measured_power"] == 1.0
        assert result["mode"] == "cc"

    # -- Net.get / device errors -----------------------------------------

    @patch("lager.Net.get")
    def test_cc_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.eload import eload_set

        with pytest.raises(RuntimeError, match="device not found"):
            eload_set(net="eload1", mode="cc")

    @patch("lager.Net.get")
    def test_cv_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.eload import eload_set

        with pytest.raises(RuntimeError, match="device not found"):
            eload_set(net="eload1", mode="cv")

    @patch("lager.Net.get")
    def test_cr_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.eload import eload_set

        with pytest.raises(RuntimeError, match="device not found"):
            eload_set(net="eload1", mode="cr")

    @patch("lager.Net.get")
    def test_cp_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.eload import eload_set

        with pytest.raises(RuntimeError, match="device not found"):
            eload_set(net="eload1", mode="cp")

    @patch("lager.Net.get")
    def test_state_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.eload import eload_state

        with pytest.raises(RuntimeError, match="device not found"):
            eload_state(net="eload1")
