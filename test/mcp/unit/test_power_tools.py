# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP power supply tools (lager.mcp.tools.power)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
@pytest.mark.power
class TestPowerTools:
    """Verify each power supply tool builds the correct lager CLI command."""

    # -- voltage ---------------------------------------------------------

    def test_voltage_read(self, mock_subprocess):
        from lager.mcp.tools.power import lager_supply_voltage
        lager_supply_voltage(box="X", net="psu1")
        assert_lager_called_with(
            mock_subprocess, "supply", "psu1", "voltage", "--box", "X",
        )

    def test_voltage_set(self, mock_subprocess):
        from lager.mcp.tools.power import lager_supply_voltage
        lager_supply_voltage(box="X", net="psu1", voltage=3.3)
        assert_lager_called_with(
            mock_subprocess,
            "supply", "psu1", "voltage", "3.3", "--yes", "--box", "X",
        )

    def test_voltage_set_with_ocp_and_ovp(self, mock_subprocess):
        from lager.mcp.tools.power import lager_supply_voltage
        lager_supply_voltage(box="X", net="psu1", voltage=5.0, ocp=1.5, ovp=6.0)
        assert_lager_called_with(
            mock_subprocess,
            "supply", "psu1", "voltage", "5.0", "--yes", "--box", "X",
            "--ocp", "1.5", "--ovp", "6.0",
        )

    # -- current ---------------------------------------------------------

    def test_current_read(self, mock_subprocess):
        from lager.mcp.tools.power import lager_supply_current
        lager_supply_current(box="X", net="psu1")
        assert_lager_called_with(
            mock_subprocess, "supply", "psu1", "current", "--box", "X",
        )

    def test_current_set(self, mock_subprocess):
        from lager.mcp.tools.power import lager_supply_current
        lager_supply_current(box="X", net="psu1", current=0.5)
        assert_lager_called_with(
            mock_subprocess,
            "supply", "psu1", "current", "0.5", "--yes", "--box", "X",
        )

    # -- enable / disable ------------------------------------------------

    def test_enable(self, mock_subprocess):
        from lager.mcp.tools.power import lager_supply_enable
        lager_supply_enable(box="X", net="psu1")
        assert_lager_called_with(
            mock_subprocess, "supply", "psu1", "enable", "--yes", "--box", "X",
        )

    def test_disable(self, mock_subprocess):
        from lager.mcp.tools.power import lager_supply_disable
        lager_supply_disable(box="X", net="psu1")
        assert_lager_called_with(
            mock_subprocess, "supply", "psu1", "disable", "--yes", "--box", "X",
        )

    # -- state -----------------------------------------------------------

    def test_state(self, mock_subprocess):
        from lager.mcp.tools.power import lager_supply_state
        lager_supply_state(box="X", net="psu1")
        assert_lager_called_with(
            mock_subprocess, "supply", "psu1", "state", "--box", "X",
        )

    # -- clear faults ----------------------------------------------------

    def test_clear_ocp(self, mock_subprocess):
        from lager.mcp.tools.power import lager_supply_clear_ocp
        lager_supply_clear_ocp(box="X", net="psu1")
        assert_lager_called_with(
            mock_subprocess, "supply", "psu1", "clear-ocp", "--box", "X",
        )

    def test_clear_ovp(self, mock_subprocess):
        from lager.mcp.tools.power import lager_supply_clear_ovp
        lager_supply_clear_ovp(box="X", net="psu1")
        assert_lager_called_with(
            mock_subprocess, "supply", "psu1", "clear-ovp", "--box", "X",
        )

    # -- set (apply config) ----------------------------------------------

    def test_set(self, mock_subprocess):
        from lager.mcp.tools.power import lager_supply_set
        lager_supply_set(box="X", net="psu1")
        assert_lager_called_with(
            mock_subprocess, "supply", "psu1", "set", "--box", "X",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_voltage_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.power import lager_supply_voltage
        result = lager_supply_voltage(box="B", net="psu1")
        assert "Error" in result

    def test_current_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.power import lager_supply_current
        result = lager_supply_current(box="B", net="psu1")
        assert "Error" in result

    def test_enable_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.power import lager_supply_enable
        result = lager_supply_enable(box="B", net="psu1")
        assert "Error" in result

    def test_disable_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.power import lager_supply_disable
        result = lager_supply_disable(box="B", net="psu1")
        assert "Error" in result

    def test_state_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.power import lager_supply_state
        result = lager_supply_state(box="B", net="psu1")
        assert "Error" in result

    def test_clear_ocp_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.power import lager_supply_clear_ocp
        result = lager_supply_clear_ocp(box="B", net="psu1")
        assert "Error" in result

    def test_clear_ovp_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.power import lager_supply_clear_ovp
        result = lager_supply_clear_ovp(box="B", net="psu1")
        assert "Error" in result

    def test_set_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.power import lager_supply_set
        result = lager_supply_set(box="B", net="psu1")
        assert "Error" in result
