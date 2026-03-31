# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for BluFi MCP tools (lager.mcp.tools.blufi) — Central and _get_client."""

import sys
import json
from unittest.mock import MagicMock, patch

# lager.protocols.ble imports bleak; dev/test envs may not have it installed.
sys.modules.setdefault("bleak", MagicMock())

import pytest


class _FakeBleDevice:
    def __init__(self, name, address, rssi=-50):
        self.name = name
        self.address = address
        self.rssi = rssi


@pytest.mark.unit
class TestBlufiTools:
    """Verify BluFi tools call Central / mocked client via _get_client and return JSON."""

    # -- scan --------------------------------

    @patch("lager.protocols.ble.Central")
    def test_scan_defaults(self, mock_central_cls):
        central = MagicMock()
        mock_central_cls.return_value = central
        central.scan.return_value = []
        from lager.mcp.tools.blufi import blufi_scan

        result = json.loads(blufi_scan())
        central.scan.assert_called_once_with(scan_time=10.0)
        assert result["status"] == "ok"
        assert result["count"] == 0

    @patch("lager.protocols.ble.Central")
    def test_scan_with_name_contains(self, mock_central_cls):
        central = MagicMock()
        mock_central_cls.return_value = central
        central.scan.return_value = [
            _FakeBleDevice("ESP32_blufi", "aa:bb:cc:dd:ee:ff"),
            _FakeBleDevice("other", "00:11:22:33:44:55"),
        ]
        from lager.mcp.tools.blufi import blufi_scan

        result = json.loads(blufi_scan(timeout=15.0, name_contains="ESP"))
        central.scan.assert_called_once_with(scan_time=15.0)
        assert result["count"] == 1
        assert "ESP" in result["devices"][0]["name"]

    @patch("lager.protocols.ble.Central")
    def test_scan_name_contains_none_omitted(self, mock_central_cls):
        central = MagicMock()
        mock_central_cls.return_value = central
        central.scan.return_value = []
        from lager.mcp.tools.blufi import blufi_scan

        json.loads(blufi_scan(timeout=5.0, name_contains=None))
        central.scan.assert_called_once_with(scan_time=5.0)

    # -- connect --------------------------------

    @patch("lager.mcp.tools.blufi._get_client")
    def test_connect_defaults(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        client.get_version.return_value = "1.0"
        client.get_wifi_status.return_value = {"connected": True}
        from lager.mcp.tools.blufi import blufi_connect

        result = json.loads(blufi_connect(device_name="BLUFI_DEV"))
        client.connect.assert_called_once_with("BLUFI_DEV", timeout=20.0)
        client.negotiate_security.assert_called_once()
        assert result["status"] == "ok"
        assert result["device"] == "BLUFI_DEV"
        assert result["connected"] is True

    @patch("lager.mcp.tools.blufi._get_client")
    def test_connect_custom_timeout(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        from lager.mcp.tools.blufi import blufi_connect

        json.loads(blufi_connect(device_name="BLUFI_DEV", timeout=30.0))
        client.connect.assert_called_once_with("BLUFI_DEV", timeout=30.0)

    # -- provision --------------------------------

    @patch("lager.mcp.tools.blufi._get_client")
    def test_provision(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        from lager.mcp.tools.blufi import blufi_provision

        result = json.loads(
            blufi_provision(
                device_name="BLUFI_DEV",
                ssid="MyWiFi",
                password="secret123",
            )
        )
        client.connect.assert_called_once_with("BLUFI_DEV", timeout=20.0)
        client.set_wifi_mode.assert_called_once_with("sta")
        client.send_wifi_credentials.assert_called_once_with("MyWiFi", "secret123")
        assert result["provisioned"] is True
        assert result["ssid"] == "MyWiFi"

    @patch("lager.mcp.tools.blufi._get_client")
    def test_provision_custom_timeout(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        from lager.mcp.tools.blufi import blufi_provision

        json.loads(
            blufi_provision(
                device_name="BLUFI_DEV",
                ssid="Net",
                password="pass",
                timeout=60.0,
            )
        )
        client.connect.assert_called_once_with("BLUFI_DEV", timeout=60.0)

    # -- wifi-scan --------------------------------

    @patch("lager.mcp.tools.blufi._get_client")
    def test_wifi_scan_defaults(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        client.wifi_scan.return_value = [{"ssid": "n1", "rssi": -70}]
        from lager.mcp.tools.blufi import blufi_wifi_scan

        result = json.loads(blufi_wifi_scan(device_name="BLUFI_DEV"))
        client.wifi_scan.assert_called_once_with(timeout=15.0)
        assert result["networks"] == [{"ssid": "n1", "rssi": -70}]

    @patch("lager.mcp.tools.blufi._get_client")
    def test_wifi_scan_custom_timeouts(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        client.wifi_scan.return_value = []
        from lager.mcp.tools.blufi import blufi_wifi_scan

        json.loads(
            blufi_wifi_scan(
                device_name="BLUFI_DEV",
                timeout=30.0,
                scan_timeout=25.0,
            )
        )
        client.connect.assert_called_once_with("BLUFI_DEV", timeout=30.0)
        client.wifi_scan.assert_called_once_with(timeout=25.0)

    # -- status --------------------------------

    @patch("lager.mcp.tools.blufi._get_client")
    def test_status(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        client.get_wifi_status.return_value = {"sta_connected": True}
        from lager.mcp.tools.blufi import blufi_status

        result = json.loads(blufi_status(device_name="BLUFI_DEV"))
        client.get_wifi_status.assert_called_once()
        assert result["wifi_status"] == {"sta_connected": True}

    # -- version --------------------------------

    @patch("lager.mcp.tools.blufi._get_client")
    def test_version(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        client.get_version.return_value = "v2.1"
        from lager.mcp.tools.blufi import blufi_version

        result = json.loads(blufi_version(device_name="BLUFI_DEV"))
        assert result["version"] == "v2.1"

    # -- error handling --------------------------------

    @patch("lager.protocols.ble.Central")
    def test_scan_raises_when_scan_fails(self, mock_central_cls):
        central = MagicMock()
        mock_central_cls.return_value = central
        central.scan.side_effect = RuntimeError("scan failed")
        from lager.mcp.tools.blufi import blufi_scan

        with pytest.raises(RuntimeError, match="scan failed"):
            blufi_scan()

    @patch("lager.mcp.tools.blufi._get_client")
    def test_connect_raises_when_connect_fails(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        client.connect.side_effect = RuntimeError("connection failed")
        from lager.mcp.tools.blufi import blufi_connect

        with pytest.raises(RuntimeError, match="connection failed"):
            blufi_connect(device_name="X")

    @patch("lager.mcp.tools.blufi._get_client")
    def test_provision_raises_when_connect_fails(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        client.connect.side_effect = RuntimeError("provision failed")
        from lager.mcp.tools.blufi import blufi_provision

        with pytest.raises(RuntimeError, match="provision failed"):
            blufi_provision(device_name="X", ssid="s", password="p")

    @patch("lager.mcp.tools.blufi._get_client")
    def test_wifi_scan_raises_when_connect_fails(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        client.connect.side_effect = RuntimeError("wifi scan failed")
        from lager.mcp.tools.blufi import blufi_wifi_scan

        with pytest.raises(RuntimeError, match="wifi scan failed"):
            blufi_wifi_scan(device_name="X")

    @patch("lager.mcp.tools.blufi._get_client")
    def test_status_raises_when_negotiate_fails(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        client.negotiate_security.side_effect = RuntimeError("status failed")
        from lager.mcp.tools.blufi import blufi_status

        with pytest.raises(RuntimeError, match="status failed"):
            blufi_status(device_name="X")

    @patch("lager.mcp.tools.blufi._get_client")
    def test_version_raises_when_get_version_fails(self, mock_get_client):
        client = MagicMock()
        mock_get_client.return_value = client
        client.get_version.side_effect = RuntimeError("version failed")
        from lager.mcp.tools.blufi import blufi_version

        with pytest.raises(RuntimeError, match="version failed"):
            blufi_version(device_name="X")
