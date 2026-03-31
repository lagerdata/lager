# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for webcam streaming via direct on-box Net API."""

import json
import socket

from ..server import mcp


def _box_ip() -> str:
    """Best-effort detection of this box's routable IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@mcp.tool()
def webcam_start(net: str) -> str:
    """Start a webcam stream on the box.

    Returns the URL where the stream can be viewed.

    Args:
        net: Webcam net name (e.g., 'camera1')
    """
    from lager import Net, NetType

    cam = Net.get(net, type=NetType.Webcam)
    result = cam.start(box_ip=_box_ip())
    return json.dumps({"status": "ok", "net": net, **result})


@mcp.tool()
def webcam_stop(net: str) -> str:
    """Stop a webcam stream on the box.

    Args:
        net: Webcam net name (e.g., 'camera1')
    """
    from lager import Net, NetType

    stopped = Net.get(net, type=NetType.Webcam).stop()
    return json.dumps({"status": "ok", "net": net, "stopped": stopped})


@mcp.tool()
def webcam_url(net: str) -> str:
    """Get the streaming URL for a webcam on the box.

    Args:
        net: Webcam net name (e.g., 'camera1')
    """
    from lager import Net, NetType

    url = Net.get(net, type=NetType.Webcam).get_url(box_ip=_box_ip())
    return json.dumps({"status": "ok", "net": net, "url": url})


@mcp.tool()
def webcam_info(net: str) -> str:
    """Get stream information for a webcam.

    Args:
        net: Webcam net name (e.g., 'camera1')
    """
    from lager import Net, NetType

    cam = Net.get(net, type=NetType.Webcam)
    info = cam.get_info(box_ip=_box_ip())
    resp = {"status": "ok", "net": net, "active": cam.is_active()}
    if info:
        resp["info"] = info
    return json.dumps(resp)
