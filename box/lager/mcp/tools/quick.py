# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for quick, interactive hardware reads and writes.

These are thin wrappers that auto-detect net type and call the appropriate
lager.Net method.  For interactive debugging — not for test authoring.
"""

from __future__ import annotations

import json
import traceback

from ..server import mcp


_READ_DISPATCH = {
    "power-supply": lambda net: {"voltage": net.voltage(), "current": net.current()},
    "power-supply-2q": lambda net: {"voltage": net.voltage(), "current": net.current()},
    "gpio": lambda net: {"level": net.input()},
    "adc": lambda net: {"voltage": net.input()},
    "thermocouple": lambda net: {"temperature_c": net.read()},
    "watt-meter": lambda net: net.read_all() if hasattr(net, "read_all") else {"power_w": net.read()},
    "battery": lambda net: {"terminal_voltage": net.terminal_voltage(), "current": net.current()},
    "eload": lambda net: {"voltage": net.measured_voltage(), "current": net.measured_current()},
}

_WRITE_DISPATCH = {
    "power-supply": "_write_power_supply",
    "power-supply-2q": "_write_power_supply",
    "gpio": "_write_gpio",
    "dac": "_write_dac",
}


def _write_power_supply(net, value):
    net.set_voltage(float(value))
    return {"set_voltage": float(value)}


def _write_gpio(net, value):
    net.output(int(value))
    return {"set_level": int(value)}


def _write_dac(net, value):
    net.output(float(value))
    return {"set_voltage": float(value)}


_WRITE_FNS = {
    "_write_power_supply": _write_power_supply,
    "_write_gpio": _write_gpio,
    "_write_dac": _write_dac,
}


@mcp.tool()
def quick_read(net_name: str) -> str:
    """Read the current value of a net (auto-detects type).

    Works for power supplies (voltage/current), GPIO (level), ADC (voltage),
    thermocouples (temperature), watt meters (power), batteries, and eloads.

    Use for quick spot-checks during interactive debugging — NOT inside
    test scripts (use the lager.Net API directly in scripts).

    Args:
        net_name: The net to read (e.g., "supply1", "gpio3", "adc0").
    """
    from ..server_state import get_bench

    bench = get_bench()
    net_desc = next((n for n in bench.nets if n.name == net_name), None)
    if not net_desc:
        return json.dumps({"error": f"Net '{net_name}' not found."})

    reader = _READ_DISPATCH.get(net_desc.net_type)
    if not reader:
        return json.dumps({
            "error": f"quick_read does not support net type '{net_desc.net_type}'.",
            "hint": "Use run_test_script() to write a Python script that reads this net.",
        })

    try:
        from lager import Net, NetType
        hw = Net.get(net_name, type=NetType(net_desc.net_type))
        result = reader(hw)
        return json.dumps({"net": net_name, "type": net_desc.net_type, **result})
    except Exception:
        return json.dumps({"error": traceback.format_exc()})


@mcp.tool()
def quick_write(net_name: str, value: str) -> str:
    """Set a value on a net (auto-detects type).

    Works for power supplies (set voltage), GPIO (set level 0/1), and
    DAC (set voltage).

    Use for quick interactive poking — NOT inside test scripts.

    Args:
        net_name: The net to write (e.g., "supply1", "gpio3", "dac0").
        value: The value to set (voltage in V, or GPIO level 0/1).
    """
    from ..server_state import get_bench

    bench = get_bench()
    net_desc = next((n for n in bench.nets if n.name == net_name), None)
    if not net_desc:
        return json.dumps({"error": f"Net '{net_name}' not found."})

    fn_name = _WRITE_DISPATCH.get(net_desc.net_type)
    if not fn_name:
        return json.dumps({
            "error": f"quick_write does not support net type '{net_desc.net_type}'.",
            "hint": "Use run_test_script() to write a Python script that controls this net.",
        })

    try:
        from lager import Net, NetType
        hw = Net.get(net_name, type=NetType(net_desc.net_type))
        result = _WRITE_FNS[fn_name](hw, value)
        return json.dumps({"net": net_name, "type": net_desc.net_type, **result})
    except Exception:
        return json.dumps({"error": traceback.format_exc()})
