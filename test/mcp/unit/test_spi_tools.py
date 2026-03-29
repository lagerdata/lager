# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SPI MCP tools -- verify CLI command construction."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestSpiTools:
    """Test all 5 SPI tool functions build the correct lager CLI commands."""

    def test_list_nets(self, mock_subprocess):
        from lager.mcp.tools.spi import lager_spi_list_nets
        lager_spi_list_nets(box="DEMO")
        assert_lager_called_with(mock_subprocess, "spi", "--box", "DEMO")

    def test_transfer_minimal(self, mock_subprocess):
        from lager.mcp.tools.spi import lager_spi_transfer
        lager_spi_transfer(box="DEMO", net="spi1", num_words=4)
        assert_lager_called_with(
            mock_subprocess,
            "spi", "spi1", "transfer", "4",
            "--format", "hex", "--box", "DEMO",
        )

    def test_transfer_all_options(self, mock_subprocess):
        from lager.mcp.tools.spi import lager_spi_transfer
        lager_spi_transfer(
            box="DEMO", net="spi1", num_words=4,
            data="0x9f", mode="0", frequency="1M",
        )
        assert_lager_called_with(
            mock_subprocess,
            "spi", "spi1", "transfer", "4",
            "--format", "hex", "--box", "DEMO",
            "--data", "0x9f", "--mode", "0", "--frequency", "1M",
        )

    def test_read_defaults(self, mock_subprocess):
        from lager.mcp.tools.spi import lager_spi_read
        lager_spi_read(box="DEMO", net="spi1", num_words=8)
        assert_lager_called_with(
            mock_subprocess,
            "spi", "spi1", "read", "8",
            "--fill", "0xFF", "--format", "hex", "--box", "DEMO",
        )

    def test_read_custom_fill(self, mock_subprocess):
        from lager.mcp.tools.spi import lager_spi_read
        lager_spi_read(box="DEMO", net="spi1", num_words=16, fill="0x00")
        assert_lager_called_with(
            mock_subprocess,
            "spi", "spi1", "read", "16",
            "--fill", "0x00", "--format", "hex", "--box", "DEMO",
        )

    def test_write(self, mock_subprocess):
        from lager.mcp.tools.spi import lager_spi_write
        lager_spi_write(box="DEMO", net="spi1", data="0x9f01020304")
        assert_lager_called_with(
            mock_subprocess,
            "spi", "spi1", "write", "0x9f01020304",
            "--format", "hex", "--box", "DEMO",
        )

    def test_config_all_options(self, mock_subprocess):
        from lager.mcp.tools.spi import lager_spi_config
        lager_spi_config(
            box="DEMO", net="spi1",
            mode="0", frequency="500k",
            bit_order="msb", word_size="8", cs_active="low",
        )
        assert_lager_called_with(
            mock_subprocess,
            "spi", "spi1", "config", "--box", "DEMO",
            "--mode", "0", "--frequency", "500k",
            "--bit-order", "msb", "--word-size", "8", "--cs-active", "low",
        )

    def test_config_no_options(self, mock_subprocess):
        from lager.mcp.tools.spi import lager_spi_config
        lager_spi_config(box="DEMO", net="spi1")
        assert_lager_called_with(
            mock_subprocess,
            "spi", "spi1", "config", "--box", "DEMO",
        )

    # -- error handling --------------------------------

    def test_spi_transfer_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.spi import lager_spi_transfer
        result = lager_spi_transfer(box="B", net="spi1", num_words=4, data="0xAB")
        assert "Error" in result

    def test_spi_read_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.spi import lager_spi_read
        result = lager_spi_read(box="B", net="spi1", num_words=4)
        assert "Error" in result

    def test_spi_write_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.spi import lager_spi_write
        result = lager_spi_write(box="B", net="spi1", data="0xAB")
        assert "Error" in result

    def test_spi_config_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.spi import lager_spi_config
        result = lager_spi_config(box="B", net="spi1", mode="0")
        assert "Error" in result
