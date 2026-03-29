# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for WiFi MCP tools -- verify CLI command construction."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestWifiTools:
    """Test all 4 WiFi tool functions build the correct lager CLI commands."""

    def test_status(self, mock_subprocess):
        from lager.mcp.tools.wifi import lager_wifi_status
        lager_wifi_status(box="DEMO")
        assert_lager_called_with(
            mock_subprocess,
            "wifi", "status", "--box", "DEMO",
        )

    def test_scan_default_interface(self, mock_subprocess):
        from lager.mcp.tools.wifi import lager_wifi_scan
        lager_wifi_scan(box="DEMO")
        assert_lager_called_with(
            mock_subprocess,
            "wifi", "access-points",
            "--interface", "wlan0", "--box", "DEMO",
        )

    def test_scan_custom_interface(self, mock_subprocess):
        from lager.mcp.tools.wifi import lager_wifi_scan
        lager_wifi_scan(box="DEMO", interface="wlan1")
        assert_lager_called_with(
            mock_subprocess,
            "wifi", "access-points",
            "--interface", "wlan1", "--box", "DEMO",
        )

    def test_connect_ssid_only(self, mock_subprocess):
        from lager.mcp.tools.wifi import lager_wifi_connect
        lager_wifi_connect(box="DEMO", ssid="OpenNet")
        assert_lager_called_with(
            mock_subprocess,
            "wifi", "connect", "--ssid", "OpenNet", "--box", "DEMO",
        )

    def test_connect_with_password_and_interface(self, mock_subprocess):
        from lager.mcp.tools.wifi import lager_wifi_connect
        lager_wifi_connect(
            box="DEMO", ssid="MyWiFi",
            password="secret123", interface="wlan1",
        )
        assert_lager_called_with(
            mock_subprocess,
            "wifi", "connect", "--ssid", "MyWiFi", "--box", "DEMO",
            "--password", "secret123", "--interface", "wlan1",
        )

    def test_delete(self, mock_subprocess):
        from lager.mcp.tools.wifi import lager_wifi_delete
        lager_wifi_delete(box="DEMO", ssid="OldNetwork")
        assert_lager_called_with(
            mock_subprocess,
            "wifi", "delete-connection", "OldNetwork",
            "--yes", "--box", "DEMO",
        )

    # -- error handling --------------------------------

    def test_wifi_status_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.wifi import lager_wifi_status
        result = lager_wifi_status(box="B")
        assert "Error" in result

    def test_wifi_scan_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.wifi import lager_wifi_scan
        result = lager_wifi_scan(box="B")
        assert "Error" in result

    def test_wifi_connect_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.wifi import lager_wifi_connect
        result = lager_wifi_connect(box="B", ssid="test")
        assert "Error" in result

    def test_wifi_delete_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.wifi import lager_wifi_delete
        result = lager_wifi_delete(box="B", ssid="test")
        assert "Error" in result
