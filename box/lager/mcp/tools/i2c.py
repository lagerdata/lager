# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for I2C communication via direct on-box Net API."""

import json

from ..server import mcp


@mcp.tool()
def i2c_scan(net: str) -> str:
    """Scan an I2C bus for connected devices.

    Args:
        net: I2C net name (e.g., 'i2c1')
    """
    from lager import Net, NetType

    addresses = Net.get(net, type=NetType.I2C).scan()
    hex_addrs = [hex(a) for a in addresses] if addresses else []
    return json.dumps({"status": "ok", "net": net, "addresses": hex_addrs, "count": len(hex_addrs)})


@mcp.tool()
def i2c_read(net: str, address: int, num_bytes: int) -> str:
    """Read bytes from an I2C device.

    Args:
        net: I2C net name (e.g., 'i2c1')
        address: 7-bit device address (e.g., 0x48)
        num_bytes: Number of bytes to read
    """
    from lager import Net, NetType

    rx = Net.get(net, type=NetType.I2C).read(address, num_bytes)
    return json.dumps({"status": "ok", "net": net, "address": hex(address), "rx_data": rx}, default=str)


@mcp.tool()
def i2c_write(net: str, address: int, data: list[int]) -> str:
    """Write bytes to an I2C device.

    Args:
        net: I2C net name (e.g., 'i2c1')
        address: 7-bit device address (e.g., 0x48)
        data: List of bytes to write (e.g., [0x0A, 0x03])
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.I2C).write(address, data)
    return json.dumps({"status": "ok", "net": net, "address": hex(address), "data": data})


@mcp.tool()
def i2c_write_read(net: str, address: int, data: list[int], num_bytes: int) -> str:
    """Write then read in a single I2C transaction (repeated start).

    Common use: write a register address, then read the register value.

    Args:
        net: I2C net name (e.g., 'i2c1')
        address: 7-bit device address (e.g., 0x48)
        data: Bytes to write first (e.g., [0x0A] for register address)
        num_bytes: Number of bytes to read after the write
    """
    from lager import Net, NetType

    rx = Net.get(net, type=NetType.I2C).write_read(address, data, num_bytes)
    return json.dumps({
        "status": "ok",
        "net": net,
        "address": hex(address),
        "tx_data": data,
        "rx_data": rx,
    }, default=str)


@mcp.tool()
def i2c_config(net: str, frequency_hz: int | None = None, pull_ups: bool | None = None) -> str:
    """Configure I2C bus parameters.

    Args:
        net: I2C net name (e.g., 'i2c1')
        frequency_hz: Clock frequency in Hz (e.g., 100000 for 100kHz, 400000 for 400kHz)
        pull_ups: Enable internal pull-up resistors (Aardvark adapters only)
    """
    from lager import Net, NetType

    cfg = {}
    if frequency_hz is not None:
        cfg["frequency_hz"] = frequency_hz
    if pull_ups is not None:
        cfg["pull_ups"] = pull_ups

    Net.get(net, type=NetType.I2C).config(**cfg)
    return json.dumps({"status": "ok", "net": net, "config": cfg})
