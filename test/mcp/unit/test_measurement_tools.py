# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP measurement tools (lager.mcp.tools.measurement)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
@pytest.mark.measurement
class TestMeasurementTools:
    """Verify each measurement tool builds the correct lager CLI command."""

    # -- ADC -------------------------------------------------------------

    def test_adc_read(self, mock_subprocess):
        from lager.mcp.tools.measurement import lager_adc_read
        lager_adc_read(box="X", net="adc1")
        assert_lager_called_with(
            mock_subprocess, "adc", "adc1", "--box", "X",
        )

    # -- DAC -------------------------------------------------------------

    def test_dac_read(self, mock_subprocess):
        from lager.mcp.tools.measurement import lager_dac_write
        lager_dac_write(box="X", net="dac1")
        assert_lager_called_with(
            mock_subprocess, "dac", "dac1", "--box", "X",
        )

    def test_dac_set_voltage(self, mock_subprocess):
        from lager.mcp.tools.measurement import lager_dac_write
        lager_dac_write(box="X", net="dac1", voltage=2.5)
        assert_lager_called_with(
            mock_subprocess, "dac", "dac1", "2.5", "--box", "X",
        )

    # -- GPI -------------------------------------------------------------

    def test_gpi_read(self, mock_subprocess):
        from lager.mcp.tools.measurement import lager_gpi_read
        lager_gpi_read(box="X", net="gpio1")
        assert_lager_called_with(
            mock_subprocess, "gpi", "gpio1", "--box", "X",
        )

    # -- GPO -------------------------------------------------------------

    def test_gpo_set_high(self, mock_subprocess):
        from lager.mcp.tools.measurement import lager_gpo_set
        lager_gpo_set(box="X", net="gpio1", level="high")
        assert_lager_called_with(
            mock_subprocess, "gpo", "gpio1", "high", "--box", "X",
        )

    def test_gpo_set_with_hold(self, mock_subprocess):
        from lager.mcp.tools.measurement import lager_gpo_set
        lager_gpo_set(box="X", net="gpio1", level="low", hold=True)
        assert_lager_called_with(
            mock_subprocess, "gpo", "gpio1", "low", "--hold", "--box", "X",
        )

    # -- Thermocouple ----------------------------------------------------

    def test_thermocouple_read(self, mock_subprocess):
        from lager.mcp.tools.measurement import lager_thermocouple_read
        lager_thermocouple_read(box="X", net="tc1")
        assert_lager_called_with(
            mock_subprocess, "thermocouple", "tc1", "--box", "X",
        )

    # -- Watt meter ------------------------------------------------------

    def test_watt_read(self, mock_subprocess):
        from lager.mcp.tools.measurement import lager_watt_read
        lager_watt_read(box="X", net="watt1")
        assert_lager_called_with(
            mock_subprocess, "watt", "watt1", "--box", "X",
        )

    # -- GPI wait-for ----------------------------------------------------

    def test_gpi_wait_for_default_timeout(self, mock_subprocess):
        from lager.mcp.tools.measurement import lager_gpi_wait_for
        lager_gpi_wait_for(box="X", net="gpio1", level="high")
        assert_lager_called_with(
            mock_subprocess,
            "gpi", "gpio1", "--wait-for", "high",
            "--timeout", "30.0", "--box", "X",
        )

    def test_gpi_wait_for_custom_timeout(self, mock_subprocess):
        from lager.mcp.tools.measurement import lager_gpi_wait_for
        lager_gpi_wait_for(box="X", net="gpio1", level="0", timeout=5.0)
        assert_lager_called_with(
            mock_subprocess,
            "gpi", "gpio1", "--wait-for", "0",
            "--timeout", "5.0", "--box", "X",
        )

    # -- error handling --------------------------------

    def test_adc_read_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.measurement import lager_adc_read
        result = lager_adc_read(box="B", net="adc1")
        assert "Error" in result

    def test_dac_write_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.measurement import lager_dac_write
        result = lager_dac_write(box="B", net="dac1", voltage=2.5)
        assert "Error" in result

    def test_gpi_read_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.measurement import lager_gpi_read
        result = lager_gpi_read(box="B", net="gpio1")
        assert "Error" in result

    def test_gpo_set_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.measurement import lager_gpo_set
        result = lager_gpo_set(box="B", net="gpio1", level="high")
        assert "Error" in result

    def test_thermocouple_read_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.measurement import lager_thermocouple_read
        result = lager_thermocouple_read(box="B", net="tc1")
        assert "Error" in result

    def test_watt_read_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.measurement import lager_watt_read
        result = lager_watt_read(box="B", net="watt1")
        assert "Error" in result
