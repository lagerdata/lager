# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for USB MCP tools -- verify CLI command construction."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestUsbTools:
    """Test all 3 USB tool functions build the correct lager CLI commands."""

    def test_enable(self, mock_subprocess):
        from lager.mcp.tools.usb import lager_usb_enable
        lager_usb_enable(box="DEMO", net="usb1")
        assert_lager_called_with(
            mock_subprocess,
            "usb", "usb1", "enable", "--box", "DEMO",
        )

    def test_disable(self, mock_subprocess):
        from lager.mcp.tools.usb import lager_usb_disable
        lager_usb_disable(box="DEMO", net="usb1")
        assert_lager_called_with(
            mock_subprocess,
            "usb", "usb1", "disable", "--box", "DEMO",
        )

    def test_toggle(self, mock_subprocess):
        from lager.mcp.tools.usb import lager_usb_toggle
        lager_usb_toggle(box="DEMO", net="usb1")
        assert_lager_called_with(
            mock_subprocess,
            "usb", "usb1", "toggle", "--box", "DEMO",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_enable_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.usb import lager_usb_enable
        result = lager_usb_enable(box="B", net="usb1")
        assert "Error" in result

    def test_disable_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.usb import lager_usb_disable
        result = lager_usb_disable(box="B", net="usb1")
        assert "Error" in result

    def test_toggle_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.usb import lager_usb_toggle
        result = lager_usb_toggle(box="B", net="usb1")
        assert "Error" in result
