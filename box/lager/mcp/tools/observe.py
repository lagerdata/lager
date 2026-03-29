# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP observation tools -- logs and power measurements."""

from __future__ import annotations

import json

from ..server import mcp, run_lager


@mcp.tool()
def read_logs(box: str = "", uart_net: str = "", lines: int = 100) -> str:
    """Read recent UART/debug logs from the DUT.

    Args:
        box: Box name (leave empty for configured box).
        uart_net: UART net name (leave empty to auto-select).
        lines: Number of lines to return (default: 100).
    """
    from ..server_state import get_bench
    from ..config import resolve_box_name

    box_name = box or resolve_box_name()
    if not box_name:
        return json.dumps({"error": "No box configured."})

    if not uart_net:
        bench = get_bench()
        for net in bench.nets:
            if net.net_type == "uart":
                uart_net = net.name
                break
    if not uart_net:
        return json.dumps({"error": "No UART net found on this bench."})

    output = run_lager("uart", uart_net, "read", "--lines", str(lines), "--box", box_name)
    return json.dumps({"net": uart_net, "lines": lines, "output": output})


@mcp.tool()
def measure_power(box: str = "", supply_net: str = "") -> str:
    """Take a single power measurement (voltage, current, power).

    Args:
        box: Box name (leave empty for configured box).
        supply_net: Power supply net name (leave empty to auto-select).
    """
    from ..server_state import get_bench
    from ..config import resolve_box_name

    box_name = box or resolve_box_name()
    if not box_name:
        return json.dumps({"error": "No box configured."})

    if not supply_net:
        bench = get_bench()
        for net in bench.nets:
            if net.net_type in ("power-supply", "battery", "watt-meter"):
                supply_net = net.name
                break
    if not supply_net:
        return json.dumps({"error": "No power/supply net found on this bench."})

    output = run_lager("supply", supply_net, "state", "--box", box_name)
    return json.dumps({"net": supply_net, "output": output})
