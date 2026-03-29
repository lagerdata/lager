# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for webcam streaming and snapshots."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_webcam_start(box: str, net: str = "") -> str:
    """Start a webcam stream on the box.

    Returns the URL where the stream can be viewed.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Webcam net name (optional, uses default if omitted)
    """
    if net:
        return run_lager("webcam", net, "start", "--box", box)
    return run_lager("webcam", "start", "--box", box)


@mcp.tool()
def lager_webcam_stop(box: str, net: str = "") -> str:
    """Stop a webcam stream on the box.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Webcam net name (optional, uses default if omitted)
    """
    if net:
        return run_lager("webcam", net, "stop", "--box", box)
    return run_lager("webcam", "stop", "--box", box)


@mcp.tool()
def lager_webcam_url(box: str, net: str = "") -> str:
    """Get the streaming URL for a webcam on the box.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Webcam net name (optional, uses default if omitted)
    """
    if net:
        return run_lager("webcam", net, "url", "--box", box)
    return run_lager("webcam", "url", "--box", box)


@mcp.tool()
def lager_webcam_start_all(box: str) -> str:
    """Start all webcam streams on a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("webcam", "start-all", "--box", box)


@mcp.tool()
def lager_webcam_stop_all(box: str) -> str:
    """Stop all webcam streams on a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("webcam", "stop-all", "--box", box)
