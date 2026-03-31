# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for Bluetooth Low Energy (BLE) device interaction.

BLE uses the box's built-in radio — there is no instrument net or
NetType for it.  The tools import the ``lager.protocols.ble.Central``
class directly.
"""

import json

from ..server import mcp


@mcp.tool()
def ble_scan(
    timeout: float = 5.0,
    name_contains: str = "",
    name_exact: str = "",
) -> str:
    """Scan for nearby BLE devices.

    Performs a BLE scan from the box and returns discovered devices.

    Args:
        timeout: Scan duration in seconds (default 5.0)
        name_contains: Filter to devices whose name contains this string
        name_exact: Filter to devices whose name matches exactly
    """
    from lager.protocols.ble import Central

    central = Central()
    devices = central.scan(scan_time=timeout, name=name_exact or None)

    results = []
    for dev in devices:
        if name_contains and (not dev.name or name_contains not in dev.name):
            continue
        results.append({
            "name": dev.name or "Unknown",
            "address": dev.address,
            "rssi": getattr(dev, "rssi", None),
        })

    return json.dumps({"status": "ok", "count": len(results), "devices": results})


@mcp.tool()
def ble_info(address: str) -> str:
    """Get detailed information about a BLE device.

    Connects briefly to the device and returns its services and
    characteristics.

    Args:
        address: BLE device address (e.g., 'AA:BB:CC:DD:EE:FF')
    """
    from bleak import BleakClient
    import asyncio

    async def _get_info():
        async with BleakClient(address) as client:
            services_list = []
            for service in client.services:
                chars = []
                for char in service.characteristics:
                    chars.append({
                        "uuid": char.uuid,
                        "properties": char.properties,
                    })
                services_list.append({
                    "uuid": service.uuid,
                    "description": service.description or "",
                    "characteristics": chars,
                })
            return services_list

    services = asyncio.run(_get_info())
    return json.dumps({"status": "ok", "address": address, "services": services})


@mcp.tool()
def ble_connect(address: str) -> str:
    """Connect to a BLE device.

    Establishes a persistent connection to the specified BLE device.

    Args:
        address: BLE device address (e.g., 'AA:BB:CC:DD:EE:FF')
    """
    from lager.protocols.ble import Central

    central = Central()
    central.connect(address)
    return json.dumps({"status": "ok", "address": address, "connected": True})


@mcp.tool()
def ble_disconnect(address: str) -> str:
    """Disconnect from a BLE device.

    Args:
        address: BLE device address (e.g., 'AA:BB:CC:DD:EE:FF')
    """
    from bleak import BleakClient
    import asyncio

    async def _disconnect():
        client = BleakClient(address)
        try:
            await client.disconnect()
        except Exception:
            pass

    asyncio.run(_disconnect())
    return json.dumps({"status": "ok", "address": address, "connected": False})
