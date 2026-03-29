# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for UART serial port access."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_uart_list_nets(box: str) -> str:
    """List available UART nets on a box.

    Shows all configured UART nets with their bridge type, device path,
    baudrate, format (e.g., 8N1), and flow control settings.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("uart", "--box", box)


@mcp.tool()
def lager_uart_serial_port(box: str, net: str) -> str:
    """Get the serial device path for a UART net.

    Returns the /dev/tty* path for the specified UART net on the box.

    Args:
        box: Box name (e.g., 'DEMO')
        net: UART net name (e.g., 'uart1')
    """
    return run_lager("uart", net, "serial-port", "--box", box)
