# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for BluFi MCP tools -- verify CLI command construction."""

import pytest
from unittest.mock import MagicMock
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestBlufiTools:
    """Test all 6 BluFi tool functions build the correct lager CLI commands."""

    # -- scan --------------------------------

    def test_scan_defaults(self, mock_subprocess):
        from lager.mcp.tools.blufi import lager_blufi_scan
        lager_blufi_scan(box="DEMO")
        assert_lager_called_with(
            mock_subprocess,
            "blufi", "scan", "--timeout", "10.0", "--box", "DEMO",
        )

    def test_scan_with_name_contains(self, mock_subprocess):
        from lager.mcp.tools.blufi import lager_blufi_scan
        lager_blufi_scan(box="DEMO", timeout=15.0, name_contains="ESP")
        assert_lager_called_with(
            mock_subprocess,
            "blufi", "scan", "--timeout", "15.0",
            "--name-contains", "ESP", "--box", "DEMO",
        )

    def test_scan_name_contains_none_omitted(self, mock_subprocess):
        from lager.mcp.tools.blufi import lager_blufi_scan
        lager_blufi_scan(box="DEMO", timeout=5.0, name_contains=None)
        assert_lager_called_with(
            mock_subprocess,
            "blufi", "scan", "--timeout", "5.0", "--box", "DEMO",
        )

    # -- connect --------------------------------

    def test_connect_defaults(self, mock_subprocess):
        from lager.mcp.tools.blufi import lager_blufi_connect
        lager_blufi_connect(box="DEMO", device_name="BLUFI_DEV")
        assert_lager_called_with(
            mock_subprocess,
            "blufi", "connect", "--timeout", "20.0",
            "BLUFI_DEV", "--box", "DEMO",
        )

    def test_connect_custom_timeout(self, mock_subprocess):
        from lager.mcp.tools.blufi import lager_blufi_connect
        lager_blufi_connect(box="DEMO", device_name="BLUFI_DEV", timeout=30.0)
        assert_lager_called_with(
            mock_subprocess,
            "blufi", "connect", "--timeout", "30.0",
            "BLUFI_DEV", "--box", "DEMO",
        )

    # -- provision --------------------------------

    def test_provision(self, mock_subprocess):
        from lager.mcp.tools.blufi import lager_blufi_provision
        lager_blufi_provision(
            box="DEMO", device_name="BLUFI_DEV",
            ssid="MyWiFi", password="secret123",
        )
        assert_lager_called_with(
            mock_subprocess,
            "blufi", "provision", "--timeout", "20.0",
            "--ssid", "MyWiFi", "--password", "secret123",
            "BLUFI_DEV", "--box", "DEMO",
        )

    def test_provision_custom_timeout(self, mock_subprocess):
        from lager.mcp.tools.blufi import lager_blufi_provision
        lager_blufi_provision(
            box="DEMO", device_name="BLUFI_DEV",
            ssid="Net", password="pass", timeout=60.0,
        )
        assert_lager_called_with(
            mock_subprocess,
            "blufi", "provision", "--timeout", "60.0",
            "--ssid", "Net", "--password", "pass",
            "BLUFI_DEV", "--box", "DEMO",
        )

    # -- wifi-scan --------------------------------

    def test_wifi_scan_defaults(self, mock_subprocess):
        from lager.mcp.tools.blufi import lager_blufi_wifi_scan
        lager_blufi_wifi_scan(box="DEMO", device_name="BLUFI_DEV")
        assert_lager_called_with(
            mock_subprocess,
            "blufi", "wifi-scan", "--timeout", "20.0",
            "--scan-timeout", "15.0",
            "BLUFI_DEV", "--box", "DEMO",
        )

    def test_wifi_scan_custom_timeouts(self, mock_subprocess):
        from lager.mcp.tools.blufi import lager_blufi_wifi_scan
        lager_blufi_wifi_scan(
            box="DEMO", device_name="BLUFI_DEV",
            timeout=30.0, scan_timeout=25.0,
        )
        assert_lager_called_with(
            mock_subprocess,
            "blufi", "wifi-scan", "--timeout", "30.0",
            "--scan-timeout", "25.0",
            "BLUFI_DEV", "--box", "DEMO",
        )

    # -- status --------------------------------

    def test_status(self, mock_subprocess):
        from lager.mcp.tools.blufi import lager_blufi_status
        lager_blufi_status(box="DEMO", device_name="BLUFI_DEV")
        assert_lager_called_with(
            mock_subprocess,
            "blufi", "status", "--timeout", "20.0",
            "BLUFI_DEV", "--box", "DEMO",
        )

    # -- version --------------------------------

    def test_version(self, mock_subprocess):
        from lager.mcp.tools.blufi import lager_blufi_version
        lager_blufi_version(box="DEMO", device_name="BLUFI_DEV")
        assert_lager_called_with(
            mock_subprocess,
            "blufi", "version", "--timeout", "20.0",
            "BLUFI_DEV", "--box", "DEMO",
        )

    # -- error handling --------------------------------

    def test_scan_subprocess_failure(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="scan failed")
        from lager.mcp.tools.blufi import lager_blufi_scan
        result = lager_blufi_scan(box="B")
        assert "Error" in result

    def test_connect_subprocess_failure(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="connection failed")
        from lager.mcp.tools.blufi import lager_blufi_connect
        result = lager_blufi_connect(box="B", device_name="X")
        assert "Error" in result

    def test_provision_subprocess_failure(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="provision failed")
        from lager.mcp.tools.blufi import lager_blufi_provision
        result = lager_blufi_provision(box="B", device_name="X", ssid="s", password="p")
        assert "Error" in result

    def test_wifi_scan_subprocess_failure(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="wifi scan failed")
        from lager.mcp.tools.blufi import lager_blufi_wifi_scan
        result = lager_blufi_wifi_scan(box="B", device_name="X")
        assert "Error" in result

    def test_status_subprocess_failure(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="status failed")
        from lager.mcp.tools.blufi import lager_blufi_status
        result = lager_blufi_status(box="B", device_name="X")
        assert "Error" in result

    def test_version_subprocess_failure(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="version failed")
        from lager.mcp.tools.blufi import lager_blufi_version
        result = lager_blufi_version(box="B", device_name="X")
        assert "Error" in result
