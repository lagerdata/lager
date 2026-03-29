# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for UART MCP tools -- verify CLI command construction."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestUartTools:
    """Test all 2 UART tool functions build the correct lager CLI commands."""

    def test_list_nets(self, mock_subprocess):
        from lager.mcp.tools.uart import lager_uart_list_nets
        lager_uart_list_nets(box="DEMO")
        assert_lager_called_with(mock_subprocess, "uart", "--box", "DEMO")

    def test_serial_port(self, mock_subprocess):
        from lager.mcp.tools.uart import lager_uart_serial_port
        lager_uart_serial_port(box="DEMO", net="uart1")
        assert_lager_called_with(
            mock_subprocess,
            "uart", "uart1", "serial-port", "--box", "DEMO",
        )

    # -- error handling --------------------------------

    def test_uart_list_nets_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.uart import lager_uart_list_nets
        result = lager_uart_list_nets(box="B")
        assert "Error" in result

    def test_uart_serial_port_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.uart import lager_uart_serial_port
        result = lager_uart_serial_port(box="B", net="uart1")
        assert "Error" in result
