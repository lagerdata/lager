# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for I2C MCP tools -- verify CLI command construction."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestI2cTools:
    """Test all 7 I2C tool functions build the correct lager CLI commands."""

    def test_list_nets(self, mock_subprocess):
        from lager.mcp.tools.i2c import lager_i2c_list_nets
        lager_i2c_list_nets(box="DEMO")
        assert_lager_called_with(mock_subprocess, "i2c", "--box", "DEMO")

    def test_scan_defaults(self, mock_subprocess):
        from lager.mcp.tools.i2c import lager_i2c_scan
        lager_i2c_scan(box="DEMO", net="i2c1")
        assert_lager_called_with(
            mock_subprocess,
            "i2c", "i2c1", "scan", "--box", "DEMO",
            "--start", "0x08", "--end", "0x77",
        )

    def test_scan_custom_range(self, mock_subprocess):
        from lager.mcp.tools.i2c import lager_i2c_scan
        lager_i2c_scan(box="BOX-A", net="i2c2", start="0x10", end="0x50")
        assert_lager_called_with(
            mock_subprocess,
            "i2c", "i2c2", "scan", "--box", "BOX-A",
            "--start", "0x10", "--end", "0x50",
        )

    def test_read(self, mock_subprocess):
        from lager.mcp.tools.i2c import lager_i2c_read
        lager_i2c_read(box="DEMO", net="i2c1", address="0x48", num_bytes=2)
        assert_lager_called_with(
            mock_subprocess,
            "i2c", "i2c1", "read", "2",
            "--address", "0x48", "--format", "hex", "--box", "DEMO",
        )

    def test_write(self, mock_subprocess):
        from lager.mcp.tools.i2c import lager_i2c_write
        lager_i2c_write(box="DEMO", net="i2c1", address="0x76", data="0x0A03")
        assert_lager_called_with(
            mock_subprocess,
            "i2c", "i2c1", "write", "0x0A03",
            "--address", "0x76", "--format", "hex", "--box", "DEMO",
        )

    def test_transfer_with_data(self, mock_subprocess):
        from lager.mcp.tools.i2c import lager_i2c_transfer
        lager_i2c_transfer(
            box="DEMO", net="i2c1", address="0x48",
            num_bytes=2, data="0x0A",
        )
        assert_lager_called_with(
            mock_subprocess,
            "i2c", "i2c1", "transfer", "2",
            "--address", "0x48", "--format", "hex", "--box", "DEMO",
            "--data", "0x0A",
        )

    def test_transfer_without_data(self, mock_subprocess):
        from lager.mcp.tools.i2c import lager_i2c_transfer
        lager_i2c_transfer(box="DEMO", net="i2c1", address="0x48", num_bytes=4)
        assert_lager_called_with(
            mock_subprocess,
            "i2c", "i2c1", "transfer", "4",
            "--address", "0x48", "--format", "hex", "--box", "DEMO",
        )

    def test_config_with_all_options(self, mock_subprocess):
        from lager.mcp.tools.i2c import lager_i2c_config
        lager_i2c_config(box="DEMO", net="i2c1", frequency="400k", pull_ups="on")
        assert_lager_called_with(
            mock_subprocess,
            "i2c", "i2c1", "config", "--box", "DEMO",
            "--frequency", "400k", "--pull-ups", "on",
        )

    def test_config_no_options(self, mock_subprocess):
        from lager.mcp.tools.i2c import lager_i2c_config
        lager_i2c_config(box="DEMO", net="i2c1")
        assert_lager_called_with(
            mock_subprocess,
            "i2c", "i2c1", "config", "--box", "DEMO",
        )

    # -- error handling --------------------------------

    def test_i2c_scan_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.i2c import lager_i2c_scan
        result = lager_i2c_scan(box="B", net="i2c1")
        assert "Error" in result

    def test_i2c_read_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.i2c import lager_i2c_read
        result = lager_i2c_read(box="B", net="i2c1", address="0x50", num_bytes=1)
        assert "Error" in result

    def test_i2c_write_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.i2c import lager_i2c_write
        result = lager_i2c_write(box="B", net="i2c1", address="0x50", data="0xFF")
        assert "Error" in result

    def test_i2c_config_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.i2c import lager_i2c_config
        result = lager_i2c_config(box="B", net="i2c1", frequency="100k")
        assert "Error" in result
