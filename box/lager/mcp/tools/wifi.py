# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for WiFi management on Lager boxes."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_wifi_status(box: str) -> str:
    """Show WiFi connection status on a Lager box.

    Returns the current WiFi connection state, SSID, IP address,
    and signal strength.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("wifi", "status", "--box", box)


@mcp.tool()
def lager_wifi_scan(box: str, interface: str = "wlan0") -> str:
    """Scan for available WiFi access points.

    Args:
        box: Box name (e.g., 'DEMO')
        interface: WiFi interface name (default: 'wlan0')
    """
    return run_lager(
        "wifi", "access-points",
        "--interface", interface,
        "--box", box,
    )


@mcp.tool()
def lager_wifi_connect(
    box: str, ssid: str,
    password: str = None, interface: str = None,
) -> str:
    """Connect a Lager box to a WiFi network.

    Args:
        box: Box name (e.g., 'DEMO')
        ssid: WiFi network name to connect to
        password: WiFi password (omit for open networks)
        interface: WiFi interface name (omit for default)
    """
    args = ["wifi", "connect", "--ssid", ssid, "--box", box]
    if password is not None:
        args.extend(["--password", password])
    if interface is not None:
        args.extend(["--interface", interface])
    return run_lager(*args)


@mcp.tool()
def lager_wifi_delete(box: str, ssid: str) -> str:
    """Delete a saved WiFi connection from a Lager box.

    Removes the saved credentials for the specified network.

    Args:
        box: Box name (e.g., 'DEMO')
        ssid: WiFi network name to delete
    """
    return run_lager(
        "wifi", "delete-connection", ssid,
        "--yes", "--box", box,
    )
