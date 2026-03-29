# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP solar simulator tools (lager.mcp.tools.solar)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestSolarTools:
    """Verify each solar simulator tool builds the correct lager CLI command."""

    # -- set / stop ------------------------------------------------------

    def test_set(self, mock_subprocess):
        from lager.mcp.tools.solar import lager_solar_set
        lager_solar_set(box="S", net="solar1")
        assert_lager_called_with(
            mock_subprocess, "solar", "solar1", "set", "--box", "S",
        )

    def test_stop(self, mock_subprocess):
        from lager.mcp.tools.solar import lager_solar_stop
        lager_solar_stop(box="S", net="solar1")
        assert_lager_called_with(
            mock_subprocess, "solar", "solar1", "stop", "--box", "S",
        )

    # -- irradiance ------------------------------------------------------

    def test_irradiance_read(self, mock_subprocess):
        from lager.mcp.tools.solar import lager_solar_irradiance
        lager_solar_irradiance(box="S", net="solar1")
        assert_lager_called_with(
            mock_subprocess, "solar", "solar1", "irradiance", "--box", "S",
        )

    def test_irradiance_set(self, mock_subprocess):
        from lager.mcp.tools.solar import lager_solar_irradiance
        lager_solar_irradiance(box="S", net="solar1", value=1000.0)
        assert_lager_called_with(
            mock_subprocess,
            "solar", "solar1", "irradiance", "1000.0", "--box", "S",
        )

    # -- mpp_current / mpp_voltage (read-only) ---------------------------

    def test_mpp_current(self, mock_subprocess):
        from lager.mcp.tools.solar import lager_solar_mpp_current
        lager_solar_mpp_current(box="S", net="solar1")
        assert_lager_called_with(
            mock_subprocess, "solar", "solar1", "mpp-current", "--box", "S",
        )

    def test_mpp_voltage(self, mock_subprocess):
        from lager.mcp.tools.solar import lager_solar_mpp_voltage
        lager_solar_mpp_voltage(box="S", net="solar1")
        assert_lager_called_with(
            mock_subprocess, "solar", "solar1", "mpp-voltage", "--box", "S",
        )

    # -- resistance ------------------------------------------------------

    def test_resistance_read(self, mock_subprocess):
        from lager.mcp.tools.solar import lager_solar_resistance
        lager_solar_resistance(box="S", net="solar1")
        assert_lager_called_with(
            mock_subprocess, "solar", "solar1", "resistance", "--box", "S",
        )

    def test_resistance_set(self, mock_subprocess):
        from lager.mcp.tools.solar import lager_solar_resistance
        lager_solar_resistance(box="S", net="solar1", value=0.5)
        assert_lager_called_with(
            mock_subprocess,
            "solar", "solar1", "resistance", "0.5", "--box", "S",
        )

    # -- temperature (read-only) -----------------------------------------

    def test_temperature(self, mock_subprocess):
        from lager.mcp.tools.solar import lager_solar_temperature
        lager_solar_temperature(box="S", net="solar1")
        assert_lager_called_with(
            mock_subprocess, "solar", "solar1", "temperature", "--box", "S",
        )

    # -- voc -------------------------------------------------------------

    def test_voc_read(self, mock_subprocess):
        from lager.mcp.tools.solar import lager_solar_voc
        lager_solar_voc(box="S", net="solar1")
        assert_lager_called_with(
            mock_subprocess, "solar", "solar1", "voc", "--box", "S",
        )

    def test_voc_set(self, mock_subprocess):
        from lager.mcp.tools.solar import lager_solar_voc
        lager_solar_voc(box="S", net="solar1", value=21.5)
        assert_lager_called_with(
            mock_subprocess,
            "solar", "solar1", "voc", "21.5", "--box", "S",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_set_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.solar import lager_solar_set
        result = lager_solar_set(box="B", net="solar1")
        assert "Error" in result

    def test_stop_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.solar import lager_solar_stop
        result = lager_solar_stop(box="B", net="solar1")
        assert "Error" in result

    def test_irradiance_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.solar import lager_solar_irradiance
        result = lager_solar_irradiance(box="B", net="solar1")
        assert "Error" in result

    def test_mpp_current_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.solar import lager_solar_mpp_current
        result = lager_solar_mpp_current(box="B", net="solar1")
        assert "Error" in result

    def test_mpp_voltage_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.solar import lager_solar_mpp_voltage
        result = lager_solar_mpp_voltage(box="B", net="solar1")
        assert "Error" in result

    def test_resistance_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.solar import lager_solar_resistance
        result = lager_solar_resistance(box="B", net="solar1")
        assert "Error" in result

    def test_temperature_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.solar import lager_solar_temperature
        result = lager_solar_temperature(box="B", net="solar1")
        assert "Error" in result

    def test_voc_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.solar import lager_solar_voc
        result = lager_solar_voc(box="B", net="solar1")
        assert "Error" in result
