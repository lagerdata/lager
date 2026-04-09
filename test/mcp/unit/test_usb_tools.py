# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for USB MCP tools (direct lager.Net API)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from lager import NetType


@pytest.mark.unit
class TestUsbTools:
    """Verify USB tools call Net.get(..., type=NetType.Usb) and the correct port methods."""

    @patch("lager.Net.get")
    def test_enable(self, mock_get):
        device = MagicMock()
        mock_get.return_value = device
        from lager.mcp.tools.usb import usb_enable

        result = json.loads(usb_enable(net="usb1"))
        mock_get.assert_called_once_with("usb1", type=NetType.Usb)
        device.enable.assert_called_once()
        assert result["status"] == "ok"
        assert result["net"] == "usb1"
        assert result["enabled"] is True

    @patch("lager.Net.get")
    def test_disable(self, mock_get):
        device = MagicMock()
        mock_get.return_value = device
        from lager.mcp.tools.usb import usb_disable

        result = json.loads(usb_disable(net="usb1"))
        mock_get.assert_called_once_with("usb1", type=NetType.Usb)
        device.disable.assert_called_once()
        assert result["status"] == "ok"
        assert result["net"] == "usb1"
        assert result["enabled"] is False

    @patch("lager.Net.get")
    def test_toggle(self, mock_get):
        device = MagicMock()
        mock_get.return_value = device
        from lager.mcp.tools.usb import usb_toggle

        result = json.loads(usb_toggle(net="usb1"))
        mock_get.assert_called_once_with("usb1", type=NetType.Usb)
        device.toggle.assert_called_once()
        assert result["status"] == "ok"
        assert result["net"] == "usb1"
        assert result["toggled"] is True

    # -- Net.get failure (hardware / resolution errors) -----------------------

    @patch("lager.Net.get")
    def test_enable_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.usb import usb_enable

        with pytest.raises(RuntimeError, match="device not found"):
            usb_enable(net="usb1")

    @patch("lager.Net.get")
    def test_disable_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.usb import usb_disable

        with pytest.raises(RuntimeError, match="device not found"):
            usb_disable(net="usb1")

    @patch("lager.Net.get")
    def test_toggle_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.usb import usb_toggle

        with pytest.raises(RuntimeError, match="device not found"):
            usb_toggle(net="usb1")
