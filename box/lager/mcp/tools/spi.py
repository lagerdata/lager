# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for SPI (Serial Peripheral Interface) communication."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_spi_list_nets(box: str) -> str:
    """List available SPI nets on a box.

    Shows all configured SPI nets with their instrument type, frequency,
    mode, and chip select settings.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("spi", "--box", box)


@mcp.tool()
def lager_spi_transfer(
    box: str, net: str, num_words: int,
    data: str = "", mode: str = "", frequency: str = "",
    format: str = "hex",
) -> str:
    """Perform an SPI transfer (simultaneous write and read, full-duplex).

    Sends data and receives response simultaneously. If data is shorter
    than num_words, pads with 0xFF. If data is longer, truncates.

    Example: Read JEDEC ID from SPI flash (send 0x9F command + 3 dummy bytes):
        data='0x9f', num_words=4

    Args:
        box: Box name (e.g., 'DEMO')
        net: SPI net name (e.g., 'spi1')
        num_words: Number of words to transfer
        data: Hex data to send (e.g., '0x9f' or '9f 00 00 00')
        mode: SPI mode 0-3 (e.g., '0')
        frequency: Clock frequency with suffix (e.g., '1M', '500k')
        format: Output format - 'hex', 'bytes', or 'json' (default: hex)
    """
    args = ["spi", net, "transfer", str(num_words), "--format", format, "--box", box]
    if data:
        args.extend(["--data", data])
    if mode:
        args.extend(["--mode", mode])
    if frequency:
        args.extend(["--frequency", frequency])
    return run_lager(*args)


@mcp.tool()
def lager_spi_read(
    box: str, net: str, num_words: int,
    fill: str = "0xFF", format: str = "hex",
) -> str:
    """Read data from an SPI slave device.

    Sends fill bytes (default 0xFF) while clocking in data from the slave.

    Args:
        box: Box name (e.g., 'DEMO')
        net: SPI net name (e.g., 'spi1')
        num_words: Number of words to read
        fill: Fill byte sent while reading (default: 0xFF)
        format: Output format - 'hex', 'bytes', or 'json' (default: hex)
    """
    return run_lager(
        "spi", net, "read", str(num_words),
        "--fill", fill, "--format", format, "--box", box,
    )


@mcp.tool()
def lager_spi_write(
    box: str, net: str, data: str,
    format: str = "hex",
) -> str:
    """Write data to an SPI slave device.

    Performs a full-duplex transfer and returns the received data.

    Args:
        box: Box name (e.g., 'DEMO')
        net: SPI net name (e.g., 'spi1')
        data: Hex data to write (e.g., '0x9f01020304' or '9f 01 02 03 04')
        format: Output format - 'hex', 'bytes', or 'json' (default: hex)
    """
    return run_lager(
        "spi", net, "write", data,
        "--format", format, "--box", box,
    )


@mcp.tool()
def lager_spi_config(
    box: str, net: str,
    mode: str = "", frequency: str = "",
    bit_order: str = "", word_size: str = "",
    cs_active: str = "",
) -> str:
    """Configure SPI bus parameters.

    Settings persist between commands for this net.

    Args:
        box: Box name (e.g., 'DEMO')
        net: SPI net name (e.g., 'spi1')
        mode: SPI mode 0-3 (CPOL/CPHA)
        frequency: Clock frequency with suffix (e.g., '1M', '500k', '5M')
        bit_order: 'msb' or 'lsb'
        word_size: Word size in bits - '8', '16', or '32'
        cs_active: Chip select polarity - 'low' or 'high'
    """
    args = ["spi", net, "config", "--box", box]
    if mode:
        args.extend(["--mode", mode])
    if frequency:
        args.extend(["--frequency", frequency])
    if bit_order:
        args.extend(["--bit-order", bit_order])
    if word_size:
        args.extend(["--word-size", word_size])
    if cs_active:
        args.extend(["--cs-active", cs_active])
    return run_lager(*args)
