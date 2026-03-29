# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP battery simulator tools (lager.mcp.tools.battery)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
@pytest.mark.battery
class TestBatteryTools:
    """Verify each battery simulator tool builds the correct lager CLI command."""

    # -- soc -------------------------------------------------------------

    def test_soc_read(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_soc
        lager_battery_soc(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "soc", "--box", "B",
        )

    def test_soc_set(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_soc
        lager_battery_soc(box="B", net="bat1", value=80.0)
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "soc", "80.0", "--box", "B",
        )

    # -- voc -------------------------------------------------------------

    def test_voc_read(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_voc
        lager_battery_voc(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "voc", "--box", "B",
        )

    def test_voc_set(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_voc
        lager_battery_voc(box="B", net="bat1", value=3.7)
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "voc", "3.7", "--box", "B",
        )

    # -- current_limit ---------------------------------------------------

    def test_current_limit_read(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_current_limit
        lager_battery_current_limit(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "current-limit", "--box", "B",
        )

    def test_current_limit_set(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_current_limit
        lager_battery_current_limit(box="B", net="bat1", value=2.0)
        assert_lager_called_with(
            mock_subprocess,
            "battery", "bat1", "current-limit", "2.0", "--box", "B",
        )

    # -- mode ------------------------------------------------------------

    def test_mode_read(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_mode
        lager_battery_mode(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "mode", "--box", "B",
        )

    def test_mode_set(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_mode
        lager_battery_mode(box="B", net="bat1", mode_type="dynamic")
        assert_lager_called_with(
            mock_subprocess,
            "battery", "bat1", "mode", "dynamic", "--box", "B",
        )

    # -- batt_full -------------------------------------------------------

    def test_batt_full_read(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_batt_full
        lager_battery_batt_full(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "batt-full", "--box", "B",
        )

    def test_batt_full_set(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_batt_full
        lager_battery_batt_full(box="B", net="bat1", voltage=4.2)
        assert_lager_called_with(
            mock_subprocess,
            "battery", "bat1", "batt-full", "4.2", "--box", "B",
        )

    # -- batt_empty ------------------------------------------------------

    def test_batt_empty_read(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_batt_empty
        lager_battery_batt_empty(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "batt-empty", "--box", "B",
        )

    def test_batt_empty_set(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_batt_empty
        lager_battery_batt_empty(box="B", net="bat1", voltage=2.8)
        assert_lager_called_with(
            mock_subprocess,
            "battery", "bat1", "batt-empty", "2.8", "--box", "B",
        )

    # -- capacity --------------------------------------------------------

    def test_capacity_read(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_capacity
        lager_battery_capacity(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "capacity", "--box", "B",
        )

    def test_capacity_set(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_capacity
        lager_battery_capacity(box="B", net="bat1", amp_hours=3.5)
        assert_lager_called_with(
            mock_subprocess,
            "battery", "bat1", "capacity", "3.5", "--box", "B",
        )

    # -- ovp -------------------------------------------------------------

    def test_ovp_read(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_ovp
        lager_battery_ovp(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "ovp", "--box", "B",
        )

    def test_ovp_set(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_ovp
        lager_battery_ovp(box="B", net="bat1", voltage=4.5)
        assert_lager_called_with(
            mock_subprocess,
            "battery", "bat1", "ovp", "4.5", "--box", "B",
        )

    # -- ocp -------------------------------------------------------------

    def test_ocp_read(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_ocp
        lager_battery_ocp(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "ocp", "--box", "B",
        )

    def test_ocp_set(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_ocp
        lager_battery_ocp(box="B", net="bat1", current=5.0)
        assert_lager_called_with(
            mock_subprocess,
            "battery", "bat1", "ocp", "5.0", "--box", "B",
        )

    # -- model -----------------------------------------------------------

    def test_model_read(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_model
        lager_battery_model(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "model", "--box", "B",
        )

    def test_model_set(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_model
        lager_battery_model(box="B", net="bat1", partnumber="NCR18650B")
        assert_lager_called_with(
            mock_subprocess,
            "battery", "bat1", "model", "NCR18650B", "--box", "B",
        )

    # -- action tools (no get/set) ---------------------------------------

    def test_enable(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_enable
        lager_battery_enable(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess,
            "battery", "bat1", "enable", "--yes", "--box", "B",
        )

    def test_disable(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_disable
        lager_battery_disable(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess,
            "battery", "bat1", "disable", "--yes", "--box", "B",
        )

    def test_state(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_state
        lager_battery_state(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "state", "--box", "B",
        )

    def test_clear_ocp(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_clear_ocp
        lager_battery_clear_ocp(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "clear-ocp", "--box", "B",
        )

    def test_clear_ovp(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_clear_ovp
        lager_battery_clear_ovp(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "clear-ovp", "--box", "B",
        )

    def test_set(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_set
        lager_battery_set(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "set", "--box", "B",
        )

    def test_clear(self, mock_subprocess):
        from lager.mcp.tools.battery import lager_battery_clear
        lager_battery_clear(box="B", net="bat1")
        assert_lager_called_with(
            mock_subprocess, "battery", "bat1", "clear", "--box", "B",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_soc_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_soc
        result = lager_battery_soc(box="B", net="bat1")
        assert "Error" in result

    def test_voc_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_voc
        result = lager_battery_voc(box="B", net="bat1")
        assert "Error" in result

    def test_enable_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_enable
        result = lager_battery_enable(box="B", net="bat1")
        assert "Error" in result

    def test_disable_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_disable
        result = lager_battery_disable(box="B", net="bat1")
        assert "Error" in result

    def test_state_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_state
        result = lager_battery_state(box="B", net="bat1")
        assert "Error" in result

    def test_current_limit_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_current_limit
        result = lager_battery_current_limit(box="B", net="bat1")
        assert "Error" in result

    def test_mode_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_mode
        result = lager_battery_mode(box="B", net="bat1")
        assert "Error" in result

    def test_capacity_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_capacity
        result = lager_battery_capacity(box="B", net="bat1")
        assert "Error" in result

    def test_ovp_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_ovp
        result = lager_battery_ovp(box="B", net="bat1")
        assert "Error" in result

    def test_ocp_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_ocp
        result = lager_battery_ocp(box="B", net="bat1")
        assert "Error" in result

    def test_batt_full_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_batt_full
        result = lager_battery_batt_full(box="B", net="bat1")
        assert "Error" in result

    def test_batt_empty_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_batt_empty
        result = lager_battery_batt_empty(box="B", net="bat1")
        assert "Error" in result

    def test_model_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_model
        result = lager_battery_model(box="B", net="bat1")
        assert "Error" in result

    def test_clear_ocp_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_clear_ocp
        result = lager_battery_clear_ocp(box="B", net="bat1")
        assert "Error" in result

    def test_clear_ovp_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_clear_ovp
        result = lager_battery_clear_ovp(box="B", net="bat1")
        assert "Error" in result

    def test_set_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_set
        result = lager_battery_set(box="B", net="bat1")
        assert "Error" in result

    def test_clear_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.battery import lager_battery_clear
        result = lager_battery_clear(box="B", net="bat1")
        assert "Error" in result
