# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for I2C MCP tools (lager.mcp.tools.i2c) — Net API."""

import json
from unittest.mock import patch, MagicMock

import pytest
from lager import NetType


@pytest.mark.unit
class TestI2cTools:
    """Verify each I2C tool calls the correct Net API."""

    @patch("lager.Net.get")
    def test_scan_returns_addresses(self, mock_get):
        i2c = MagicMock()
        i2c.scan.return_value = [0x48, 0x76]
        mock_get.return_value = i2c
        from lager.mcp.tools.i2c import i2c_scan

        result = json.loads(i2c_scan(net="i2c1"))
        mock_get.assert_called_once_with("i2c1", type=NetType.I2C)
        i2c.scan.assert_called_once_with()
        assert result["status"] == "ok"
        assert result["net"] == "i2c1"
        assert result["addresses"] == ["0x48", "0x76"]
        assert result["count"] == 2

    @patch("lager.Net.get")
    def test_scan_empty_bus(self, mock_get):
        i2c = MagicMock()
        i2c.scan.return_value = []
        mock_get.return_value = i2c
        from lager.mcp.tools.i2c import i2c_scan

        result = json.loads(i2c_scan(net="i2c2"))
        assert result["addresses"] == []
        assert result["count"] == 0

    @patch("lager.Net.get")
    def test_read(self, mock_get):
        i2c = MagicMock()
        i2c.read.return_value = [0x12, 0x34]
        mock_get.return_value = i2c
        from lager.mcp.tools.i2c import i2c_read

        result = json.loads(i2c_read(net="i2c1", address=0x48, num_bytes=2))
        mock_get.assert_called_once_with("i2c1", type=NetType.I2C)
        i2c.read.assert_called_once_with(0x48, 2)
        assert result["status"] == "ok"
        assert result["address"] == "0x48"
        assert result["rx_data"] == [0x12, 0x34]

    @patch("lager.Net.get")
    def test_write(self, mock_get):
        i2c = MagicMock()
        mock_get.return_value = i2c
        from lager.mcp.tools.i2c import i2c_write

        data = [0x0A, 0x03]
        result = json.loads(i2c_write(net="i2c1", address=0x76, data=data))
        i2c.write.assert_called_once_with(0x76, data)
        assert result["status"] == "ok"
        assert result["address"] == "0x76"
        assert result["data"] == data

    @patch("lager.Net.get")
    def test_write_read(self, mock_get):
        i2c = MagicMock()
        i2c.write_read.return_value = [0xBE, 0xEF]
        mock_get.return_value = i2c
        from lager.mcp.tools.i2c import i2c_write_read

        result = json.loads(
            i2c_write_read(net="i2c1", address=0x48, data=[0x0A], num_bytes=2)
        )
        i2c.write_read.assert_called_once_with(0x48, [0x0A], 2)
        assert result["status"] == "ok"
        assert result["tx_data"] == [0x0A]
        assert result["rx_data"] == [0xBE, 0xEF]

    @patch("lager.Net.get")
    def test_config_with_all_options(self, mock_get):
        i2c = MagicMock()
        mock_get.return_value = i2c
        from lager.mcp.tools.i2c import i2c_config

        result = json.loads(i2c_config(net="i2c1", frequency_hz=400_000, pull_ups=True))
        i2c.config.assert_called_once_with(frequency_hz=400_000, pull_ups=True)
        assert result["status"] == "ok"
        assert result["config"] == {"frequency_hz": 400_000, "pull_ups": True}

    @patch("lager.Net.get")
    def test_config_no_options(self, mock_get):
        i2c = MagicMock()
        mock_get.return_value = i2c
        from lager.mcp.tools.i2c import i2c_config

        result = json.loads(i2c_config(net="i2c1"))
        i2c.config.assert_called_once_with()
        assert result["status"] == "ok"
        assert result["config"] == {}

    # -- error handling --------------------------------

    @patch("lager.Net.get")
    def test_i2c_scan_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.i2c import i2c_scan

        with pytest.raises(RuntimeError, match="device not found"):
            i2c_scan(net="i2c1")

    @patch("lager.Net.get")
    def test_i2c_read_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.i2c import i2c_read

        with pytest.raises(RuntimeError, match="device not found"):
            i2c_read(net="i2c1", address=0x50, num_bytes=1)

    @patch("lager.Net.get")
    def test_i2c_write_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.i2c import i2c_write

        with pytest.raises(RuntimeError, match="device not found"):
            i2c_write(net="i2c1", address=0x50, data=[0xFF])

    @patch("lager.Net.get")
    def test_i2c_config_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.i2c import i2c_config

        with pytest.raises(RuntimeError, match="device not found"):
            i2c_config(net="i2c1", frequency_hz=100_000)
