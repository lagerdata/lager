# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for SPI communication via direct on-box Net API."""

import json

from ..server import mcp


@mcp.tool()
def spi_transfer(net: str, data: list[int], num_words: int = 0) -> str:
    """Full-duplex SPI transfer (simultaneous write and read).

    Args:
        net: SPI net name (e.g., 'spi1')
        data: List of bytes to send (e.g., [0x9f, 0x00, 0x00, 0x00])
        num_words: Number of words to transfer (0 = same as data length)
    """
    from lager import Net, NetType

    spi = Net.get(net, type=NetType.SPI)
    if num_words and num_words > len(data):
        data = data + [0xFF] * (num_words - len(data))
    rx = spi.read_write(data)
    return json.dumps({"status": "ok", "net": net, "tx_data": data, "rx_data": rx}, default=str)


@mcp.tool()
def spi_read(net: str, num_words: int, fill: int = 0xFF) -> str:
    """Read data from SPI bus (sends fill bytes while clocking in).

    Args:
        net: SPI net name (e.g., 'spi1')
        num_words: Number of words to read
        fill: Fill byte sent while reading (default: 0xFF)
    """
    from lager import Net, NetType

    rx = Net.get(net, type=NetType.SPI).read(num_words, fill=fill)
    return json.dumps({"status": "ok", "net": net, "rx_data": rx}, default=str)


@mcp.tool()
def spi_write(net: str, data: list[int]) -> str:
    """Write data to SPI bus (discard read data).

    Args:
        net: SPI net name (e.g., 'spi1')
        data: List of bytes to write (e.g., [0x06, 0x01, 0x02])
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.SPI).write(data)
    return json.dumps({"status": "ok", "net": net, "data": data})


@mcp.tool()
def spi_config(
    net: str,
    mode: int | None = None,
    frequency_hz: int | None = None,
    bit_order: str | None = None,
    word_size: int | None = None,
) -> str:
    """Configure SPI bus parameters.

    Args:
        net: SPI net name (e.g., 'spi1')
        mode: SPI mode 0-3 (CPOL/CPHA)
        frequency_hz: Clock frequency in Hz (e.g., 1000000 for 1MHz)
        bit_order: 'msb' or 'lsb'
        word_size: Word size in bits (8, 16, or 32)
    """
    from lager import Net, NetType

    cfg = {}
    if mode is not None:
        cfg["mode"] = mode
    if frequency_hz is not None:
        cfg["frequency_hz"] = frequency_hz
    if bit_order is not None:
        cfg["bit_order"] = bit_order
    if word_size is not None:
        cfg["word_size"] = word_size

    Net.get(net, type=NetType.SPI).config(**cfg)
    return json.dumps({"status": "ok", "net": net, "config": cfg})
