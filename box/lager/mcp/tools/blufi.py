# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for BluFi (ESP32 WiFi provisioning over BLE)."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_blufi_scan(
    box: str,
    timeout: float = 10.0,
    name_contains: str = None,
) -> str:
    """Scan for BluFi-capable BLE devices.

    Scans for BLE devices advertising the BluFi service UUID or matching
    a name filter. Used to discover ESP32 devices ready for WiFi provisioning.

    Args:
        box: Box name (e.g., 'DEMO')
        timeout: Scan duration in seconds (default 10.0)
        name_contains: Filter to devices whose name contains this string
    """
    args = ["blufi", "scan", "--timeout", str(timeout)]
    if name_contains is not None:
        args.extend(["--name-contains", name_contains])
    args.extend(["--box", box])
    return run_lager(*args, timeout=120)


@mcp.tool()
def lager_blufi_connect(
    box: str,
    device_name: str,
    timeout: float = 20.0,
) -> str:
    """Connect to a BluFi device and retrieve its version and WiFi status.

    Establishes a BLE connection, negotiates security, and queries the
    device for firmware version and current WiFi connection state.

    Args:
        box: Box name (e.g., 'DEMO')
        device_name: BLE device name to connect to (e.g., 'BLUFI_DEVICE')
        timeout: BLE connection timeout in seconds (default 20.0)
    """
    args = ["blufi", "connect", "--timeout", str(timeout), device_name]
    args.extend(["--box", box])
    return run_lager(*args, timeout=120)


@mcp.tool()
def lager_blufi_provision(
    box: str,
    device_name: str,
    ssid: str,
    password: str,
    timeout: float = 20.0,
) -> str:
    """Provision WiFi credentials to a BluFi device.

    Connects to the device, sets it to STA mode, sends WiFi credentials,
    and verifies the device connects to the specified network.

    Args:
        box: Box name (e.g., 'DEMO')
        device_name: BLE device name to connect to (e.g., 'BLUFI_DEVICE')
        ssid: WiFi network SSID
        password: WiFi network password
        timeout: BLE connection timeout in seconds (default 20.0)
    """
    args = ["blufi", "provision", "--timeout", str(timeout),
            "--ssid", ssid, "--password", password, device_name]
    args.extend(["--box", box])
    return run_lager(*args, timeout=120)


@mcp.tool()
def lager_blufi_wifi_scan(
    box: str,
    device_name: str,
    timeout: float = 20.0,
    scan_timeout: float = 15.0,
) -> str:
    """Scan for WiFi networks via a BluFi device.

    Connects to the BluFi device and instructs it to scan for nearby WiFi
    networks. Returns a list of SSIDs with signal strength (RSSI).

    Args:
        box: Box name (e.g., 'DEMO')
        device_name: BLE device name to connect to (e.g., 'BLUFI_DEVICE')
        timeout: BLE connection timeout in seconds (default 20.0)
        scan_timeout: WiFi scan duration on device in seconds (default 15.0)
    """
    args = ["blufi", "wifi-scan", "--timeout", str(timeout),
            "--scan-timeout", str(scan_timeout), device_name]
    args.extend(["--box", box])
    return run_lager(*args, timeout=120)


@mcp.tool()
def lager_blufi_status(
    box: str,
    device_name: str,
    timeout: float = 20.0,
) -> str:
    """Get WiFi connection status from a BluFi device.

    Connects to the device and queries its current WiFi state including
    operating mode and station connection status.

    Args:
        box: Box name (e.g., 'DEMO')
        device_name: BLE device name to connect to (e.g., 'BLUFI_DEVICE')
        timeout: BLE connection timeout in seconds (default 20.0)
    """
    args = ["blufi", "status", "--timeout", str(timeout), device_name]
    args.extend(["--box", box])
    return run_lager(*args, timeout=120)


@mcp.tool()
def lager_blufi_version(
    box: str,
    device_name: str,
    timeout: float = 20.0,
) -> str:
    """Get firmware version from a BluFi device.

    Connects to the device and retrieves its BluFi firmware version.

    Args:
        box: Box name (e.g., 'DEMO')
        device_name: BLE device name to connect to (e.g., 'BLUFI_DEVICE')
        timeout: BLE connection timeout in seconds (default 20.0)
    """
    args = ["blufi", "version", "--timeout", str(timeout), device_name]
    args.extend(["--box", box])
    return run_lager(*args, timeout=120)
