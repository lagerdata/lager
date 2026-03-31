# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for firmware debugging, RTT, and GDB server via direct on-box Net API."""

import json
import time

from ..server import mcp


@mcp.tool()
def debug_flash(net: str, firmware_path: str) -> str:
    """Flash firmware to the DUT via the debug probe.

    Args:
        net: Debug net name (e.g., 'debug1')
        firmware_path: Path to firmware file (.elf, .hex, .bin) on the box filesystem
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Debug).flash(firmware_path)
    return json.dumps({"status": "ok", "net": net, "firmware_path": firmware_path})


@mcp.tool()
def debug_reset(net: str, halt: bool = False) -> str:
    """Reset the DUT via the debug probe.

    Args:
        net: Debug net name (e.g., 'debug1')
        halt: Halt the CPU after reset (default: false)
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Debug).reset(halt=halt)
    return json.dumps({"status": "ok", "net": net, "halt": halt})


@mcp.tool()
def debug_erase(net: str) -> str:
    """Erase the DUT's flash memory.

    WARNING: The device will have no firmware after this operation.

    Args:
        net: Debug net name (e.g., 'debug1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Debug).erase()
    return json.dumps({"status": "ok", "net": net, "erased": True})


@mcp.tool()
def debug_connect(net: str, speed: int = 0) -> str:
    """Connect the debug probe to the DUT.

    Args:
        net: Debug net name (e.g., 'debug1')
        speed: SWD/JTAG speed in kHz (0 = auto)
    """
    from lager import Net, NetType

    kwargs = {}
    if speed:
        kwargs["speed"] = speed
    Net.get(net, type=NetType.Debug).connect(**kwargs)
    return json.dumps({"status": "ok", "net": net, "connected": True})


@mcp.tool()
def debug_disconnect(net: str) -> str:
    """Disconnect the debug probe from the DUT.

    Args:
        net: Debug net name (e.g., 'debug1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Debug).disconnect()
    return json.dumps({"status": "ok", "net": net, "connected": False})


@mcp.tool()
def debug_read_memory(net: str, address: int, length: int = 4) -> str:
    """Read a region of DUT memory.

    Args:
        net: Debug net name (e.g., 'debug1')
        address: Start address (e.g., 0x08000000)
        length: Number of bytes to read (default: 4)
    """
    from lager import Net, NetType

    data = Net.get(net, type=NetType.Debug).read_memory(address, length)
    return json.dumps({
        "status": "ok",
        "net": net,
        "address": hex(address),
        "length": length,
        "data": data,
    }, default=str)


# ── RTT (Real-Time Transfer) tools ──────────────────────────────────

_rtt_sessions: dict = {}


def _get_rtt(net: str, channel: int = 0):
    """Get or create a cached RTT session via the debug net's context manager."""
    from lager import Net, NetType

    key = f"{net}:{channel}"
    if key not in _rtt_sessions:
        dbg = Net.get(net, type=NetType.Debug)
        ctx = dbg.rtt(channel=channel)
        _rtt_sessions[key] = ctx.__enter__()
    return _rtt_sessions[key]


@mcp.tool()
def rtt_write(net: str, data: str, channel: int = 0) -> str:
    """Write data to a DUT RTT channel.

    Requires an active debug connection. The RTT session is created
    automatically on first use and cached for subsequent calls.

    Args:
        net: Debug net name (e.g., 'debug1')
        data: UTF-8 string to send
        channel: RTT channel number (default: 0)
    """
    rtt = _get_rtt(net, channel)
    rtt.write(data.encode("utf-8", errors="ignore"))
    return json.dumps({"status": "ok", "net": net, "channel": channel, "bytes_written": len(data)})


@mcp.tool()
def rtt_read(net: str, channel: int = 0, timeout_ms: int = 1000) -> str:
    """Read available data from a DUT RTT channel.

    Returns whatever data is available within the timeout window.
    Does not wait for a specific pattern — use rtt_expect() for that.

    Args:
        net: Debug net name (e.g., 'debug1')
        channel: RTT channel number (default: 0)
        timeout_ms: Read timeout in milliseconds (default: 1000)
    """
    rtt = _get_rtt(net, channel)
    timeout_s = timeout_ms / 1000.0
    chunks: list[str] = []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        raw = rtt.read_some(timeout=min(0.25, deadline - time.time()))
        if raw:
            chunks.append(raw.decode("utf-8", errors="ignore"))
        elif chunks:
            break
    output = "".join(chunks)
    return json.dumps({"status": "ok", "net": net, "channel": channel, "output": output})


@mcp.tool()
def rtt_expect(net: str, pattern: str, channel: int = 0, timeout_ms: int = 5000) -> str:
    """Read from an RTT channel until a pattern matches or timeout.

    Args:
        net: Debug net name (e.g., 'debug1')
        pattern: String to search for in the RTT output
        channel: RTT channel number (default: 0)
        timeout_ms: Maximum wait time in milliseconds (default: 5000)
    """
    rtt = _get_rtt(net, channel)
    timeout_s = timeout_ms / 1000.0
    chunks: list[str] = []
    matched = False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        raw = rtt.read_some(timeout=min(0.5, deadline - time.time()))
        if raw:
            chunks.append(raw.decode("utf-8", errors="ignore"))
            if pattern in "".join(chunks):
                matched = True
                break
    output = "".join(chunks)
    return json.dumps({
        "status": "ok",
        "net": net,
        "channel": channel,
        "pattern": pattern,
        "matched": matched,
        "output": output,
    })


# ── GDB server ──────────────────────────────────────────────────────

@mcp.tool()
def debug_gdbserver(net: str, port: int = 3333) -> str:
    """Start a GDB server connected to the DUT via the debug probe.

    The GDB server listens on the specified port and can be connected
    to from ``arm-none-eabi-gdb`` or any GDB-compatible client with
    ``target remote :<port>``.

    Args:
        net: Debug net name (e.g., 'debug1')
        port: TCP port for the GDB server (default: 3333)
    """
    from lager import Net, NetType

    dbg = Net.get(net, type=NetType.Debug)
    dbg.gdbserver(port=port)
    return json.dumps({"status": "ok", "net": net, "gdbserver_port": port})
