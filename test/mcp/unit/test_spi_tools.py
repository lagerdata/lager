# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SPI MCP tools (lager.mcp.tools.spi) — Net API."""

import json
from unittest.mock import patch, MagicMock

import pytest
from lager import NetType


@pytest.mark.unit
class TestSpiTools:
    """Verify each SPI tool calls the correct Net API."""

    @patch("lager.Net.get")
    def test_transfer_minimal(self, mock_get):
        spi = MagicMock()
        spi.read_write.return_value = [0x00, 0x12, 0x34, 0x56]
        mock_get.return_value = spi
        from lager.mcp.tools.spi import spi_transfer

        data = [0x9F, 0x00, 0x00, 0x00]
        result = json.loads(spi_transfer(net="spi1", data=data))
        mock_get.assert_called_once_with("spi1", type=NetType.SPI)
        spi.read_write.assert_called_once_with(data)
        assert result["status"] == "ok"
        assert result["net"] == "spi1"
        assert result["tx_data"] == data
        assert result["rx_data"] == [0x00, 0x12, 0x34, 0x56]

    @patch("lager.Net.get")
    def test_transfer_pads_when_num_words_greater_than_data(self, mock_get):
        spi = MagicMock()
        spi.read_write.return_value = [1, 2, 3, 4]
        mock_get.return_value = spi
        from lager.mcp.tools.spi import spi_transfer

        result = json.loads(spi_transfer(net="spi1", data=[0x9F], num_words=4))
        spi.read_write.assert_called_once_with([0x9F, 0xFF, 0xFF, 0xFF])
        assert result["status"] == "ok"

    @patch("lager.Net.get")
    def test_read_defaults(self, mock_get):
        spi = MagicMock()
        spi.read.return_value = [0xAB] * 8
        mock_get.return_value = spi
        from lager.mcp.tools.spi import spi_read

        result = json.loads(spi_read(net="spi1", num_words=8))
        mock_get.assert_called_once_with("spi1", type=NetType.SPI)
        spi.read.assert_called_once_with(8, fill=0xFF)
        assert result["status"] == "ok"
        assert result["rx_data"] == [0xAB] * 8

    @patch("lager.Net.get")
    def test_read_custom_fill(self, mock_get):
        spi = MagicMock()
        spi.read.return_value = []
        mock_get.return_value = spi
        from lager.mcp.tools.spi import spi_read

        json.loads(spi_read(net="spi1", num_words=16, fill=0x00))
        spi.read.assert_called_once_with(16, fill=0x00)

    @patch("lager.Net.get")
    def test_write(self, mock_get):
        spi = MagicMock()
        mock_get.return_value = spi
        from lager.mcp.tools.spi import spi_write

        data = [0x06, 0x01, 0x02]
        result = json.loads(spi_write(net="spi1", data=data))
        spi.write.assert_called_once_with(data)
        assert result["status"] == "ok"
        assert result["data"] == data

    @patch("lager.Net.get")
    def test_config_all_options(self, mock_get):
        spi = MagicMock()
        mock_get.return_value = spi
        from lager.mcp.tools.spi import spi_config

        result = json.loads(
            spi_config(
                net="spi1",
                mode=0,
                frequency_hz=500_000,
                bit_order="msb",
                word_size=8,
            )
        )
        spi.config.assert_called_once_with(
            mode=0,
            frequency_hz=500_000,
            bit_order="msb",
            word_size=8,
        )
        assert result["status"] == "ok"
        assert result["config"] == {
            "mode": 0,
            "frequency_hz": 500_000,
            "bit_order": "msb",
            "word_size": 8,
        }

    @patch("lager.Net.get")
    def test_config_no_options(self, mock_get):
        spi = MagicMock()
        mock_get.return_value = spi
        from lager.mcp.tools.spi import spi_config

        result = json.loads(spi_config(net="spi1"))
        spi.config.assert_called_once_with()
        assert result["status"] == "ok"
        assert result["config"] == {}

    # -- error handling --------------------------------

    @patch("lager.Net.get")
    def test_spi_transfer_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.spi import spi_transfer

        with pytest.raises(RuntimeError, match="device not found"):
            spi_transfer(net="spi1", data=[0xAB])

    @patch("lager.Net.get")
    def test_spi_read_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.spi import spi_read

        with pytest.raises(RuntimeError, match="device not found"):
            spi_read(net="spi1", num_words=4)

    @patch("lager.Net.get")
    def test_spi_write_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.spi import spi_write

        with pytest.raises(RuntimeError, match="device not found"):
            spi_write(net="spi1", data=[0xAB])

    @patch("lager.Net.get")
    def test_spi_config_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.spi import spi_config

        with pytest.raises(RuntimeError, match="device not found"):
            spi_config(net="spi1", mode=0)
