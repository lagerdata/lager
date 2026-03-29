# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for USB hub port control."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_usb_enable(box: str, net: str) -> str:
    """Enable a USB hub port.

    Powers on the specified USB hub port.

    Args:
        box: Box name (e.g., 'DEMO')
        net: USB net name (e.g., 'usb1')
    """
    return run_lager("usb", net, "enable", "--box", box)


@mcp.tool()
def lager_usb_disable(box: str, net: str) -> str:
    """Disable a USB hub port.

    Powers off the specified USB hub port.

    Args:
        box: Box name (e.g., 'DEMO')
        net: USB net name (e.g., 'usb1')
    """
    return run_lager("usb", net, "disable", "--box", box)


@mcp.tool()
def lager_usb_toggle(box: str, net: str) -> str:
    """Power-cycle a USB hub port.

    Disables and re-enables the specified USB hub port to force
    a re-enumeration of the connected device.

    Args:
        box: Box name (e.g., 'DEMO')
        net: USB net name (e.g., 'usb1')
    """
    return run_lager("usb", net, "toggle", "--box", box)
