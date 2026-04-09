# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for WiFi MCP tools (lager.mcp.tools.wifi) — subprocess / nmcli / iwconfig."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestWifiTools:
    """Verify WiFi tools invoke subprocess.run correctly and return JSON."""

    @patch("lager.mcp.tools.wifi.subprocess.run")
    def test_status(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='wlan0     IEEE 802.11  ESSID:"MyNet"\n'
            "          Signal level=-50 dBm  ",
            stderr="",
        )
        from lager.mcp.tools.wifi import wifi_status

        result = json.loads(wifi_status())
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0][:2] == ["iwconfig", "wlan0"]
        assert result["status"] == "ok"
        assert result["ssid"] == "MyNet"
        assert result["state"] == "connected"

    @patch("lager.mcp.tools.wifi.subprocess.run")
    def test_scan_default_interface(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                'Cell 01 - Address: AA:BB:CC:DD:EE:01\n'
                '                    ESSID:"NetA"\n'
                "                    Signal level=-60 dBm\n"
                "                    Encryption key:off\n"
            ),
            stderr="",
        )
        from lager.mcp.tools.wifi import wifi_scan

        result = json.loads(wifi_scan())
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["iwlist", "wlan0", "scan"]
        assert result["status"] == "ok"
        assert result["interface"] == "wlan0"
        assert len(result["access_points"]) >= 1
        assert result["access_points"][0]["ssid"] == "NetA"

    @patch("lager.mcp.tools.wifi.subprocess.run")
    def test_scan_custom_interface(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                'Cell 01 - Address: 11:22:33:44:55:66\n'
                '                    ESSID:"Other"\n'
                "                    Signal level=-70 dBm\n"
            ),
            stderr="",
        )
        from lager.mcp.tools.wifi import wifi_scan

        json.loads(wifi_scan(interface="wlan1"))
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["iwlist", "wlan1", "scan"]

    @patch("lager.mcp.tools.wifi.subprocess.run")
    def test_connect_ssid_only(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="connected", stderr="")
        from lager.mcp.tools.wifi import wifi_connect

        result = json.loads(wifi_connect(ssid="OpenNet"))
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[:5] == ["nmcli", "dev", "wifi", "connect", "OpenNet"]
        assert "password" not in cmd
        assert result["connected"] is True

    @patch("lager.mcp.tools.wifi.subprocess.run")
    def test_connect_with_password_and_interface(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        from lager.mcp.tools.wifi import wifi_connect

        json.loads(
            wifi_connect(
                ssid="MyWiFi",
                password="secret123",
                interface="wlan1",
            )
        )
        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "nmcli", "dev", "wifi", "connect", "MyWiFi",
            "password", "secret123",
            "ifname", "wlan1",
        ]

    @patch("lager.mcp.tools.wifi.subprocess.run")
    def test_delete(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        from lager.mcp.tools.wifi import wifi_delete

        result = json.loads(wifi_delete(ssid="OldNetwork"))
        mock_run.assert_called_once_with(
            ["nmcli", "connection", "delete", "OldNetwork"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result["deleted"] is True

    # -- error handling --------------------------------

    @patch("lager.mcp.tools.wifi.subprocess.run")
    def test_wifi_status_subprocess_failure(self, mock_run):
        mock_run.side_effect = OSError("device not found")
        from lager.mcp.tools.wifi import wifi_status

        result = json.loads(wifi_status())
        assert result["status"] == "ok"
        assert result["state"] == "error"
        assert "device not found" in result["error"]

    @patch("lager.mcp.tools.wifi.subprocess.run")
    def test_wifi_scan_subprocess_failure(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="device not found"),
            MagicMock(returncode=1, stdout="", stderr="still bad"),
        ]
        from lager.mcp.tools.wifi import wifi_scan

        result = json.loads(wifi_scan())
        assert result["status"] == "error"
        assert "Could not scan" in result["error"]

    @patch("lager.mcp.tools.wifi.subprocess.run")
    def test_wifi_connect_subprocess_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="device not found",
        )
        from lager.mcp.tools.wifi import wifi_connect

        result = json.loads(wifi_connect(ssid="test"))
        assert result["status"] == "error"
        assert result["connected"] is False
        assert "device not found" in result["error"]

    @patch("lager.mcp.tools.wifi.subprocess.run")
    def test_wifi_connect_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="nmcli", timeout=30)
        from lager.mcp.tools.wifi import wifi_connect

        result = json.loads(wifi_connect(ssid="test"))
        assert result["status"] == "error"
        assert "timeout" in result["error"].lower()

    @patch("lager.mcp.tools.wifi.subprocess.run")
    def test_wifi_delete_subprocess_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="device not found",
        )
        from lager.mcp.tools.wifi import wifi_delete

        result = json.loads(wifi_delete(ssid="test"))
        assert result["status"] == "error"
        assert result["deleted"] is False
