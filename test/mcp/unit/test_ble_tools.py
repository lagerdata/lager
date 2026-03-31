# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for BLE MCP tools (lager.mcp.tools.ble) — Central and BleakClient."""

import sys
import json
from unittest.mock import AsyncMock, MagicMock, patch

# lager.protocols.ble imports bleak; dev/test envs may not have it installed.
sys.modules.setdefault("bleak", MagicMock())

import pytest


class _FakeBleDevice:
    def __init__(self, name, address, rssi=-42):
        self.name = name
        self.address = address
        self.rssi = rssi


@pytest.mark.unit
class TestBleTools:
    """Verify BLE tools call Central / BleakClient and return JSON."""

    @patch("lager.protocols.ble.Central")
    def test_scan_defaults(self, mock_central_cls):
        central = MagicMock()
        mock_central_cls.return_value = central
        central.scan.return_value = [
            _FakeBleDevice("DevA", "11:22:33:44:55:66"),
        ]
        from lager.mcp.tools.ble import ble_scan

        result = json.loads(ble_scan())
        central.scan.assert_called_once_with(scan_time=5.0, name=None)
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["devices"][0]["address"] == "11:22:33:44:55:66"

    @patch("lager.protocols.ble.Central")
    def test_scan_with_name_contains(self, mock_central_cls):
        central = MagicMock()
        mock_central_cls.return_value = central
        central.scan.return_value = [
            _FakeBleDevice("SensorFoo", "aa:bb:cc:dd:ee:ff"),
            _FakeBleDevice("Other", "00:11:22:33:44:55"),
        ]
        from lager.mcp.tools.ble import ble_scan

        result = json.loads(
            ble_scan(timeout=10.0, name_contains="Sensor"),
        )
        central.scan.assert_called_once_with(scan_time=10.0, name=None)
        assert result["count"] == 1
        assert "Sensor" in result["devices"][0]["name"]

    @patch("lager.protocols.ble.Central")
    def test_scan_with_name_exact(self, mock_central_cls):
        central = MagicMock()
        mock_central_cls.return_value = central
        central.scan.return_value = [_FakeBleDevice("MyDevice", "aa:bb:cc:dd:ee:ff")]
        from lager.mcp.tools.ble import ble_scan

        result = json.loads(ble_scan(name_exact="MyDevice"))
        central.scan.assert_called_once_with(scan_time=5.0, name="MyDevice")
        assert result["count"] == 1

    @patch("bleak.BleakClient")
    def test_info(self, mock_bleak_client):
        char = MagicMock()
        char.uuid = "0000fff1-0000-1000-8000-00805f9b34fb"
        char.properties = ["read", "write"]
        svc = MagicMock()
        svc.uuid = "0000fff0-0000-1000-8000-00805f9b34fb"
        svc.description = "Custom"
        svc.characteristics = [char]
        client_instance = MagicMock()
        client_instance.services = [svc]

        cm = AsyncMock()
        cm.__aenter__.return_value = client_instance
        cm.__aexit__.return_value = None
        mock_bleak_client.return_value = cm

        from lager.mcp.tools.ble import ble_info

        result = json.loads(ble_info(address="AA:BB:CC:DD:EE:FF"))
        mock_bleak_client.assert_called_once_with("AA:BB:CC:DD:EE:FF")
        assert result["status"] == "ok"
        assert result["address"] == "AA:BB:CC:DD:EE:FF"
        assert len(result["services"]) == 1
        assert result["services"][0]["uuid"] == svc.uuid

    @patch("lager.protocols.ble.Central")
    def test_connect(self, mock_central_cls):
        central = MagicMock()
        mock_central_cls.return_value = central
        from lager.mcp.tools.ble import ble_connect

        result = json.loads(ble_connect(address="AA:BB:CC:DD:EE:FF"))
        central.connect.assert_called_once_with("AA:BB:CC:DD:EE:FF")
        assert result["status"] == "ok"
        assert result["connected"] is True

    @patch("bleak.BleakClient")
    def test_disconnect(self, mock_bleak_client):
        client_instance = MagicMock()
        client_instance.disconnect = AsyncMock()
        mock_bleak_client.return_value = client_instance
        from lager.mcp.tools.ble import ble_disconnect

        result = json.loads(ble_disconnect(address="AA:BB:CC:DD:EE:FF"))
        mock_bleak_client.assert_called_once_with("AA:BB:CC:DD:EE:FF")
        client_instance.disconnect.assert_called_once()
        assert result["status"] == "ok"
        assert result["connected"] is False

    # -- error handling --------------------------------

    @patch("lager.protocols.ble.Central")
    def test_ble_scan_raises_when_scan_fails(self, mock_central_cls):
        central = MagicMock()
        mock_central_cls.return_value = central
        central.scan.side_effect = RuntimeError("adapter not found")
        from lager.mcp.tools.ble import ble_scan

        with pytest.raises(RuntimeError, match="adapter not found"):
            ble_scan()

    @patch("bleak.BleakClient")
    def test_ble_info_raises_when_client_fails(self, mock_bleak_client):
        cm = AsyncMock()
        cm.__aenter__.side_effect = OSError("device not found")
        cm.__aexit__.return_value = None
        mock_bleak_client.return_value = cm
        from lager.mcp.tools.ble import ble_info

        with pytest.raises(OSError, match="device not found"):
            ble_info(address="AA:BB:CC:DD:EE:FF")

    @patch("lager.protocols.ble.Central")
    def test_ble_connect_raises_when_connect_fails(self, mock_central_cls):
        central = MagicMock()
        mock_central_cls.return_value = central
        central.connect.side_effect = RuntimeError("connection failed")
        from lager.mcp.tools.ble import ble_connect

        with pytest.raises(RuntimeError, match="connection failed"):
            ble_connect(address="AA:BB:CC:DD:EE:FF")

    @patch("bleak.BleakClient")
    def test_ble_disconnect_swallows_disconnect_error(self, mock_bleak_client):
        client_instance = MagicMock()
        client_instance.disconnect = AsyncMock(side_effect=RuntimeError("not connected"))
        mock_bleak_client.return_value = client_instance
        from lager.mcp.tools.ble import ble_disconnect

        result = json.loads(ble_disconnect(address="AA:BB:CC:DD:EE:FF"))
        assert result["status"] == "ok"
        assert result["connected"] is False
