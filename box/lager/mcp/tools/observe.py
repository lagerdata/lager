# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP observation tools — UART logs and power measurements via direct Net API."""

import json

from ..server import mcp

_POWER_ROLE_TO_NETTYPE = {
    "power-supply": "PowerSupply",
    "power-supply-2q": "PowerSupply2Q",
    "battery": "Battery",
    "watt-meter": "WattMeter",
}


@mcp.tool()
def read_uart(net: str = "", timeout_s: float = 2.0, baudrate: int = 115200) -> str:
    """Read available data from a UART net (DUT serial output).

    Args:
        net: UART net name (leave empty to auto-select first UART net).
        timeout_s: Read timeout in seconds (default: 2.0).
        baudrate: Baud rate (default: 115200).
    """
    from lager import Net, NetType

    if not net:
        from ..server_state import get_bench
        for n in get_bench().nets:
            if n.net_type == "uart":
                net = n.name
                break
    if not net:
        return json.dumps({"status": "error", "error": "No UART net found on this bench."})

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
def measure_power(net: str = "") -> str:
    """Take a single power measurement (voltage, current, power).

    Works with power-supply, power-supply-2q, battery, and watt-meter nets.

    Args:
        net: Power-related net name (leave empty to auto-select).
    """
    from lager import Net, NetType

    resolved_role = None
    if not net:
        from ..server_state import get_bench
        for n in get_bench().nets:
            if n.net_type in _POWER_ROLE_TO_NETTYPE:
                net = n.name
                resolved_role = n.net_type
                break
    if not net:
        return json.dumps({"status": "error", "error": "No power/supply net found."})

    if not resolved_role:
        from ..server_state import get_bench
        for n in get_bench().nets:
            if n.name == net:
                resolved_role = n.net_type
                break

    net_type_name = _POWER_ROLE_TO_NETTYPE.get(resolved_role, "PowerSupply")
    net_type = getattr(NetType, net_type_name, NetType.PowerSupply)
    device = Net.get(net, type=net_type)

    result = {"status": "ok", "net": net, "net_type": resolved_role or "unknown"}
    try:
        result["voltage"] = device.voltage()
    except Exception:
        pass
    try:
        result["current"] = device.current()
    except Exception:
        pass
    try:
        result["power"] = device.power()
    except Exception:
        pass

    if net_type_name == "WattMeter":
        try:
            result["power"] = device.read()
        except Exception:
            pass

    return json.dumps(result)
