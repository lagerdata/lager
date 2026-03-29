# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for I2C (Inter-Integrated Circuit) communication."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_i2c_list_nets(box: str) -> str:
    """List available I2C nets on a box.

    Shows all configured I2C nets with their instrument type, frequency,
    and pull-up resistor settings.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("i2c", "--box", box)


@mcp.tool()
def lager_i2c_scan(box: str, net: str, start: str = "0x08", end: str = "0x77") -> str:
    """Scan an I2C bus for connected devices.

    Probes each address in the range and reports those that ACK.
    Common device addresses: 0x48 (TMP102), 0x68 (MPU6050), 0x76 (BME280).

    Args:
        box: Box name (e.g., 'DEMO')
        net: I2C net name (e.g., 'i2c1')
        start: Start address in hex (default: 0x08)
        end: End address in hex (default: 0x77)
    """
    return run_lager("i2c", net, "scan", "--box", box, "--start", start, "--end", end)


@mcp.tool()
def lager_i2c_read(box: str, net: str, address: str, num_bytes: int, format: str = "hex") -> str:
    """Read bytes from an I2C device.

    Args:
        box: Box name (e.g., 'DEMO')
        net: I2C net name (e.g., 'i2c1')
        address: 7-bit device address in hex (e.g., '0x48')
        num_bytes: Number of bytes to read
        format: Output format - 'hex', 'bytes', or 'json' (default: hex)
    """
    return run_lager(
        "i2c", net, "read", str(num_bytes),
        "--address", address, "--format", format, "--box", box,
    )


@mcp.tool()
def lager_i2c_write(box: str, net: str, address: str, data: str, format: str = "hex") -> str:
    """Write bytes to an I2C device.

    Args:
        box: Box name (e.g., 'DEMO')
        net: I2C net name (e.g., 'i2c1')
        address: 7-bit device address in hex (e.g., '0x48')
        data: Hex data to write (e.g., '0x0A03' or '0a 03' or '0a,03')
        format: Output format - 'hex', 'bytes', or 'json' (default: hex)
    """
    return run_lager(
        "i2c", net, "write", data,
        "--address", address, "--format", format, "--box", box,
    )


@mcp.tool()
def lager_i2c_transfer(
    box: str, net: str, address: str, num_bytes: int,
    data: str = "", format: str = "hex",
) -> str:
    """Write then read in a single I2C transaction (repeated start).

    Common pattern: write a register address, then read the register value
    in one atomic operation without releasing the bus.

    Example: To read 2 bytes from register 0x0A on device 0x48:
        data='0x0A', num_bytes=2, address='0x48'

    Args:
        box: Box name (e.g., 'DEMO')
        net: I2C net name (e.g., 'i2c1')
        address: 7-bit device address in hex (e.g., '0x48')
        num_bytes: Number of bytes to read after the write
        data: Hex data to write before reading (e.g., '0x0A' for register address)
        format: Output format - 'hex', 'bytes', or 'json' (default: hex)
    """
    args = ["i2c", net, "transfer", str(num_bytes), "--address", address, "--format", format, "--box", box]
    if data:
        args.extend(["--data", data])
    return run_lager(*args)


@mcp.tool()
def lager_i2c_config(
    box: str, net: str,
    frequency: str = "", pull_ups: str = "",
) -> str:
    """Configure I2C bus parameters.

    Settings persist between commands for this net.

    Args:
        box: Box name (e.g., 'DEMO')
        net: I2C net name (e.g., 'i2c1')
        frequency: Clock frequency with suffix (e.g., '100k', '400k', '1M')
        pull_ups: Enable/disable internal pull-ups - 'on' or 'off' (Aardvark only)
    """
    args = ["i2c", net, "config", "--box", box]
    if frequency:
        args.extend(["--frequency", frequency])
    if pull_ups:
        args.extend(["--pull-ups", pull_ups])
    return run_lager(*args)
