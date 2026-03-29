# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for Bluetooth Low Energy (BLE) device interaction."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_ble_scan(
    box: str,
    timeout: float = 5.0,
    name_contains: str = None,
    name_exact: str = None,
) -> str:
    """Scan for nearby BLE devices.

    Performs a BLE scan from the box and returns discovered devices.
    Optional filters can narrow results by device name.

    Args:
        box: Box name (e.g., 'DEMO')
        timeout: Scan duration in seconds (default 5.0)
        name_contains: Filter to devices whose name contains this string
        name_exact: Filter to devices whose name matches this string exactly
    """
    args = ["ble", "scan", "--timeout", str(timeout)]
    if name_contains is not None:
        args.extend(["--name-contains", name_contains])
    if name_exact is not None:
        args.extend(["--name-exact", name_exact])
    args.extend(["--box", box])
    return run_lager(*args)


@mcp.tool()
def lager_ble_info(box: str, address: str) -> str:
    """Get detailed information about a BLE device.

    Connects briefly to the device and returns its services,
    characteristics, and other metadata.

    Args:
        box: Box name (e.g., 'DEMO')
        address: BLE device address (e.g., 'AA:BB:CC:DD:EE:FF')
    """
    return run_lager("ble", "info", address, "--box", box)


@mcp.tool()
def lager_ble_connect(box: str, address: str) -> str:
    """Connect to a BLE device.

    Establishes a persistent connection to the specified BLE device.

    Args:
        box: Box name (e.g., 'DEMO')
        address: BLE device address (e.g., 'AA:BB:CC:DD:EE:FF')
    """
    return run_lager("ble", "connect", address, "--box", box)


@mcp.tool()
def lager_ble_disconnect(box: str, address: str) -> str:
    """Disconnect from a BLE device.

    Terminates the connection to the specified BLE device.

    Args:
        box: Box name (e.g., 'DEMO')
        address: BLE device address (e.g., 'AA:BB:CC:DD:EE:FF')
    """
    return run_lager("ble", "disconnect", address, "--box", box)
