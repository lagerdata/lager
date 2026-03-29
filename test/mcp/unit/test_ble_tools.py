# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for BLE MCP tools -- verify CLI command construction."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestBleTools:
    """Test all 4 BLE tool functions build the correct lager CLI commands."""

    def test_scan_defaults(self, mock_subprocess):
        from lager.mcp.tools.ble import lager_ble_scan
        lager_ble_scan(box="DEMO")
        assert_lager_called_with(
            mock_subprocess,
            "ble", "scan", "--timeout", "5.0", "--box", "DEMO",
        )

    def test_scan_with_name_contains(self, mock_subprocess):
        from lager.mcp.tools.ble import lager_ble_scan
        lager_ble_scan(box="DEMO", timeout=10.0, name_contains="Sensor")
        assert_lager_called_with(
            mock_subprocess,
            "ble", "scan", "--timeout", "10.0",
            "--name-contains", "Sensor", "--box", "DEMO",
        )

    def test_scan_with_name_exact(self, mock_subprocess):
        from lager.mcp.tools.ble import lager_ble_scan
        lager_ble_scan(box="DEMO", name_exact="MyDevice")
        assert_lager_called_with(
            mock_subprocess,
            "ble", "scan", "--timeout", "5.0",
            "--name-exact", "MyDevice", "--box", "DEMO",
        )

    def test_info(self, mock_subprocess):
        from lager.mcp.tools.ble import lager_ble_info
        lager_ble_info(box="DEMO", address="AA:BB:CC:DD:EE:FF")
        assert_lager_called_with(
            mock_subprocess,
            "ble", "info", "AA:BB:CC:DD:EE:FF", "--box", "DEMO",
        )

    def test_connect(self, mock_subprocess):
        from lager.mcp.tools.ble import lager_ble_connect
        lager_ble_connect(box="DEMO", address="AA:BB:CC:DD:EE:FF")
        assert_lager_called_with(
            mock_subprocess,
            "ble", "connect", "AA:BB:CC:DD:EE:FF", "--box", "DEMO",
        )

    def test_disconnect(self, mock_subprocess):
        from lager.mcp.tools.ble import lager_ble_disconnect
        lager_ble_disconnect(box="DEMO", address="AA:BB:CC:DD:EE:FF")
        assert_lager_called_with(
            mock_subprocess,
            "ble", "disconnect", "AA:BB:CC:DD:EE:FF", "--box", "DEMO",
        )

    # -- error handling --------------------------------

    def test_ble_scan_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.ble import lager_ble_scan
        result = lager_ble_scan(box="B")
        assert "Error" in result

    def test_ble_info_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.ble import lager_ble_info
        result = lager_ble_info(box="B", address="AA:BB:CC:DD:EE:FF")
        assert "Error" in result

    def test_ble_connect_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.ble import lager_ble_connect
        result = lager_ble_connect(box="B", address="AA:BB:CC:DD:EE:FF")
        assert "Error" in result

    def test_ble_disconnect_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.ble import lager_ble_disconnect
        result = lager_ble_disconnect(box="B", address="AA:BB:CC:DD:EE:FF")
        assert "Error" in result
