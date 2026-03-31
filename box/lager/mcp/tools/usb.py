# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for USB hub port control via direct on-box Net API."""

import json

from ..server import mcp


@mcp.tool()
def usb_enable(net: str) -> str:
    """Enable (power on) a USB hub port.

    Args:
        net: USB net name (e.g., 'usb1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Usb).enable()
    return json.dumps({"status": "ok", "net": net, "enabled": True})


@mcp.tool()
def usb_disable(net: str) -> str:
    """Disable (power off) a USB hub port.

    Args:
        net: USB net name (e.g., 'usb1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Usb).disable()
    return json.dumps({"status": "ok", "net": net, "enabled": False})


@mcp.tool()
def usb_toggle(net: str) -> str:
    """Power-cycle a USB hub port (disable then re-enable).

    Forces a re-enumeration of the connected USB device.

    Args:
        net: USB net name (e.g., 'usb1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Usb).toggle()
    return json.dumps({"status": "ok", "net": net, "toggled": True})
