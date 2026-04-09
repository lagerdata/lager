# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for UART MCP tools (lager.mcp.tools.uart) — Net API."""

import json
from unittest.mock import patch, MagicMock

import pytest
from lager import NetType


@pytest.mark.unit
class TestUartTools:
    """Verify each UART tool calls connect/serial and returns JSON."""

    @patch("lager.Net.get")
    def test_uart_send(self, mock_get):
        uart = MagicMock()
        ser = MagicMock()
        uart.connect.return_value = ser
        mock_get.return_value = uart
        from lager.mcp.tools.uart import uart_send

        result = json.loads(uart_send(net="uart1", data="hello"))
        mock_get.assert_called_once_with("uart1", type=NetType.UART)
        uart.connect.assert_called_once_with(baudrate=115200, timeout=1)
        ser.write.assert_called_once_with(b"hello")
        ser.close.assert_called_once()
        assert result["status"] == "ok"
        assert result["net"] == "uart1"
        assert result["data"] == "hello"

    @patch("lager.Net.get")
    def test_uart_send_custom_baudrate(self, mock_get):
        uart = MagicMock()
        ser = MagicMock()
        uart.connect.return_value = ser
        mock_get.return_value = uart
        from lager.mcp.tools.uart import uart_send

        json.loads(uart_send(net="uart1", data="x", baudrate=9600))
        uart.connect.assert_called_once_with(baudrate=9600, timeout=1)

    @patch("lager.Net.get")
    def test_uart_read(self, mock_get):
        uart = MagicMock()
        ser = MagicMock()
        ser.readline.side_effect = [b"line one\n", b"line two\n", b""]
        uart.connect.return_value = ser
        mock_get.return_value = uart
        from lager.mcp.tools.uart import uart_read

        result = json.loads(uart_read(net="uart1", timeout_s=1.5, baudrate=115200))
        mock_get.assert_called_once_with("uart1", type=NetType.UART)
        uart.connect.assert_called_once_with(baudrate=115200, timeout=1.5)
        assert result["lines"] == ["line one", "line two"]
        assert result["count"] == 2
        ser.close.assert_called_once()

    @patch("lager.Net.get")
    def test_uart_read_empty(self, mock_get):
        uart = MagicMock()
        ser = MagicMock()
        ser.readline.return_value = b""
        uart.connect.return_value = ser
        mock_get.return_value = uart
        from lager.mcp.tools.uart import uart_read

        result = json.loads(uart_read(net="uart1"))
        assert result["lines"] == []
        assert result["count"] == 0

    @patch("lager.Net.get")
    def test_uart_send_and_expect_matched(self, mock_get):
        uart = MagicMock()
        ser = MagicMock()
        ser.readline.return_value = b"status: READY ok\n"
        uart.connect.return_value = ser
        mock_get.return_value = uart
        from lager.mcp.tools.uart import uart_send_and_expect

        result = json.loads(
            uart_send_and_expect(
                net="uart1",
                send="ping\n",
                expect="READY",
                timeout_ms=1000,
                baudrate=115200,
            )
        )
        mock_get.assert_called_once_with("uart1", type=NetType.UART)
        uart.connect.assert_called_once_with(baudrate=115200, timeout=1.0)
        ser.reset_input_buffer.assert_called_once()
        ser.write.assert_called_once_with(b"ping\n")
        assert result["matched"] is True
        assert "READY" in result["output"][0]
        ser.close.assert_called_once()

    @patch("lager.Net.get")
    def test_uart_send_and_expect_not_matched(self, mock_get):
        uart = MagicMock()
        ser = MagicMock()
        ser.readline.return_value = b""
        uart.connect.return_value = ser
        mock_get.return_value = uart
        from lager.mcp.tools.uart import uart_send_and_expect

        result = json.loads(
            uart_send_and_expect(net="uart1", send="x", expect="NEVER", timeout_ms=50)
        )
        assert result["matched"] is False
        assert result["output"] == []

    # -- error handling --------------------------------

    @patch("lager.Net.get")
    def test_uart_send_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.uart import uart_send

        with pytest.raises(RuntimeError, match="device not found"):
            uart_send(net="uart1", data="x")

    @patch("lager.Net.get")
    def test_uart_read_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.uart import uart_read

        with pytest.raises(RuntimeError, match="device not found"):
            uart_read(net="uart1")

    @patch("lager.Net.get")
    def test_uart_send_and_expect_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.uart import uart_send_and_expect

        with pytest.raises(RuntimeError, match="device not found"):
            uart_send_and_expect(net="uart1", send="a", expect="b")
