# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tool for quick, interactive hardware reads and writes.

Thin wrapper that auto-detects net type and calls the appropriate
lager.Net method.  For interactive debugging -- not for test authoring.
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
def quick_io(net_name: str, action: str = "read", value: str | None = None) -> str:
    """Read or write a net value (auto-detects type).

    Reads: PSU (voltage/current), GPIO, ADC, thermocouple, watt-meter,
    battery, eload.  Writes: PSU (voltage), GPIO (0/1), DAC (voltage).

    Args:
        net_name: The net to interact with (e.g. "supply1", "gpio3").
        action: "read" or "write".
        value: Value to set (required for writes -- voltage in V, or 0/1 for GPIO).
    """
    from ..server_state import get_bench

    bench = get_bench()
    net_desc = next((n for n in bench.nets if n.name == net_name), None)
    if not net_desc:
        return json.dumps({"error": f"Net '{net_name}' not found."})

    if action == "read":
        reader = _READ_DISPATCH.get(net_desc.net_type)
        if not reader:
            return json.dumps({
                "error": f"quick_io read does not support net type '{net_desc.net_type}'.",
                "hint": "Use run_test_script() with a Python script instead.",
            })
        try:
            from lager import Net, NetType
            hw = Net.get(net_name, type=NetType(net_desc.net_type))
            result = reader(hw)
            return json.dumps({"net": net_name, "type": net_desc.net_type, **result})
        except Exception:
            return json.dumps({"error": traceback.format_exc()})

    elif action == "write":
        if value is None:
            return json.dumps({"error": "value is required for write action."})
        fn_name = _WRITE_DISPATCH.get(net_desc.net_type)
        if not fn_name:
            return json.dumps({
                "error": f"quick_io write does not support net type '{net_desc.net_type}'.",
                "hint": "Use run_test_script() with a Python script instead.",
            })
        try:
            from lager import Net, NetType
            hw = Net.get(net_name, type=NetType(net_desc.net_type))
            result = _WRITE_FNS[fn_name](hw, value)
            return json.dumps({"net": net_name, "type": net_desc.net_type, **result})
        except Exception:
            return json.dumps({"error": traceback.format_exc()})

    else:
        return json.dumps({"error": f"Unknown action '{action}'. Use 'read' or 'write'."})
