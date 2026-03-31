# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for UART serial communication via direct on-box Net API."""

import json

from ..server import mcp


@mcp.tool()
def uart_send(net: str, data: str, baudrate: int = 115200) -> str:
    """Send data to the DUT over UART.

    Args:
        net: UART net name (e.g., 'uart1')
        data: String data to send (raw — no newline appended; include '\\n' if needed)
        baudrate: Baud rate (default: 115200)
    """
    from lager import Net, NetType

    uart = Net.get(net, type=NetType.UART)
    ser = uart.connect(baudrate=baudrate, timeout=1)
    try:
        ser.write(data.encode("utf-8", errors="ignore"))
    finally:
        ser.close()

    return json.dumps({"status": "ok", "net": net, "data": data})


@mcp.tool()
def uart_read(net: str, timeout_s: float = 2.0, baudrate: int = 115200) -> str:
    """Read available data from a UART net.

    Reads until timeout or no more data is available.

    Args:
        net: UART net name (e.g., 'uart1')
        timeout_s: Read timeout in seconds (default: 2.0)
        baudrate: Baud rate (default: 115200)
    """
    from lager import Net, NetType

    uart = Net.get(net, type=NetType.UART)
    ser = uart.connect(baudrate=baudrate, timeout=timeout_s)

    lines = []
    try:
        while True:
            line = ser.readline()
            if not line:
                break
            lines.append(line.decode("utf-8", errors="ignore").strip())
    finally:
        ser.close()

    return json.dumps({"status": "ok", "net": net, "lines": lines, "count": len(lines)})


@mcp.tool()
def uart_send_and_expect(
    net: str,
    send: str,
    expect: str,
    timeout_ms: int = 5000,
    baudrate: int = 115200,
) -> str:
    """Send a command and wait for an expected response pattern.

    This is a single-round-trip tool combining send + read with
    pattern matching, useful for DUT CLI interactions.

    Args:
        net: UART net name (e.g., 'uart1')
        send: Command string to send
        expect: Substring pattern to match in the response
        timeout_ms: How long to wait for the pattern (default: 5000ms)
        baudrate: Baud rate (default: 115200)
    """
    import time
    from lager import Net, NetType

    uart = Net.get(net, type=NetType.UART)
    timeout_s = timeout_ms / 1000.0
    ser = uart.connect(baudrate=baudrate, timeout=timeout_s)

    try:
        ser.reset_input_buffer()
        ser.write(send.encode("utf-8", errors="ignore"))

        lines = []
        matched = False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if line:
                lines.append(line)
                if expect in line:
                    matched = True
                    break
    finally:
        ser.close()

    return json.dumps({
        "status": "ok",
        "net": net,
        "sent": send,
        "pattern": expect,
        "matched": matched,
        "output": lines,
    })
