# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for BluFi (ESP32 WiFi provisioning over BLE).

BluFi uses the box's BLE radio to communicate with ESP32 devices.
There is no instrument net — the tools import ``lager.blufi`` directly.
"""

import json

from ..server import mcp


def _get_client():
    """Create a fresh BlufiClient instance."""
    from lager.blufi.client import BlufiClient
    return BlufiClient()


@mcp.tool()
def blufi_scan(timeout: float = 10.0, name_contains: str = "") -> str:
    """Scan for BluFi-capable BLE devices.

    Looks for BLE devices advertising the BluFi service UUID or matching
    a name filter. Used to discover ESP32 devices ready for WiFi provisioning.

    Args:
        timeout: Scan duration in seconds (default 10.0)
        name_contains: Filter to devices whose name contains this string
    """
    from lager.protocols.ble import Central

    central = Central()
    devices = central.scan(scan_time=timeout)

    results = []
    for dev in devices:
        name = dev.name or ""
        if name_contains and name_contains not in name:
            continue
        results.append({
            "name": name or "Unknown",
            "address": dev.address,
            "rssi": getattr(dev, "rssi", None),
        })
    return json.dumps({"status": "ok", "count": len(results), "devices": results})


@mcp.tool()
def blufi_connect(device_name: str, timeout: float = 20.0) -> str:
    """Connect to a BluFi device and retrieve its version and WiFi status.

    Establishes a BLE connection, negotiates security, and queries the
    device for firmware version and current WiFi connection state.

    Args:
        device_name: BLE device name to connect to (e.g., 'BLUFI_DEVICE')
        timeout: BLE connection timeout in seconds (default 20.0)
    """
    client = _get_client()
    client.connect(device_name, timeout=timeout)
    client.negotiate_security()

    info = {"status": "ok", "device": device_name, "connected": True}
    try:
        info["version"] = client.get_version()
    except Exception:
        pass
    try:
        info["wifi_status"] = client.get_wifi_status()
    except Exception:
        pass
    return json.dumps(info, default=str)


@mcp.tool()
def blufi_provision(
    device_name: str,
    ssid: str,
    password: str,
    timeout: float = 20.0,
) -> str:
    """Provision WiFi credentials to a BluFi device.

    Connects to the device, sets it to STA mode, sends WiFi credentials,
    and verifies the device connects to the specified network.

    Args:
        device_name: BLE device name to connect to
        ssid: WiFi network SSID
        password: WiFi network password
        timeout: BLE connection timeout in seconds (default 20.0)
    """
    client = _get_client()
    client.connect(device_name, timeout=timeout)
    client.negotiate_security()
    client.set_wifi_mode("sta")
    client.send_wifi_credentials(ssid, password)

    return json.dumps({
        "status": "ok",
        "device": device_name,
        "ssid": ssid,
        "provisioned": True,
    })


@mcp.tool()
def blufi_wifi_scan(
    device_name: str,
    timeout: float = 20.0,
    scan_timeout: float = 15.0,
) -> str:
    """Scan for WiFi networks via a BluFi device.

    Connects to the BluFi device and instructs it to scan for nearby WiFi
    networks. Returns a list of SSIDs with signal strength (RSSI).

    Args:
        device_name: BLE device name to connect to
        timeout: BLE connection timeout in seconds (default 20.0)
        scan_timeout: WiFi scan duration on device in seconds (default 15.0)
    """
    client = _get_client()
    client.connect(device_name, timeout=timeout)
    client.negotiate_security()
    networks = client.wifi_scan(timeout=scan_timeout)

    return json.dumps({
        "status": "ok",
        "device": device_name,
        "networks": networks,
    }, default=str)


@mcp.tool()
def blufi_status(device_name: str, timeout: float = 20.0) -> str:
    """Get WiFi connection status from a BluFi device.

    Args:
        device_name: BLE device name to connect to
        timeout: BLE connection timeout in seconds (default 20.0)
    """
    client = _get_client()
    client.connect(device_name, timeout=timeout)
    client.negotiate_security()
    wifi_status = client.get_wifi_status()

    return json.dumps({
        "status": "ok",
        "device": device_name,
        "wifi_status": wifi_status,
    }, default=str)


@mcp.tool()
def blufi_version(device_name: str, timeout: float = 20.0) -> str:
    """Get firmware version from a BluFi device.

    Args:
        device_name: BLE device name to connect to
        timeout: BLE connection timeout in seconds (default 20.0)
    """
    client = _get_client()
    client.connect(device_name, timeout=timeout)
    client.negotiate_security()
    version = client.get_version()

    return json.dumps({
        "status": "ok",
        "device": device_name,
        "version": version,
    }, default=str)
