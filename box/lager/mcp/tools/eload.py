# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for electronic load control via direct on-box Net API."""

import json

from ..server import mcp


@mcp.tool()
def eload_set(net: str, mode: str, value: float = None) -> str:
    """Set electronic load mode and optionally set the value.

    Args:
        net: Electronic load net name (e.g., 'eload1')
        mode: Operating mode — 'cc' (constant current), 'cv' (constant voltage),
              'cr' (constant resistance), or 'cp' (constant power)
        value: Setpoint value (amps for cc, volts for cv, ohms for cr, watts for cp).
               Omit to read current setting for that mode.
    """
    from lager import Net, NetType

    eload = Net.get(net, type=NetType.ELoad)
    mode_map = {"cc": "current", "cv": "voltage", "cr": "resistance", "cp": "power"}
    attr_name = mode_map.get(mode.lower())
    if not attr_name:
        return json.dumps({"status": "error", "error": f"Unknown mode '{mode}'. Use cc, cv, cr, or cp."})

    result_value = getattr(eload, attr_name)(value)
    resp = {"status": "ok", "net": net, "mode": mode.lower()}
    if value is not None:
        resp[attr_name] = value
    elif result_value is not None:
        resp[attr_name] = result_value
    return json.dumps(resp)


@mcp.tool()
def eload_enable(net: str) -> str:
    """Enable (turn on) the electronic load input.

    Args:
        net: Electronic load net name (e.g., 'eload1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.ELoad).enable()
    return json.dumps({"status": "ok", "net": net, "enabled": True})


@mcp.tool()
def eload_disable(net: str) -> str:
    """Disable (turn off) the electronic load input.

    Args:
        net: Electronic load net name (e.g., 'eload1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.ELoad).disable()
    return json.dumps({"status": "ok", "net": net, "enabled": False})


@mcp.tool()
def eload_state(net: str) -> str:
    """Read the current state of an electronic load.

    Returns measured voltage, current, and power.

    Args:
        net: Electronic load net name (e.g., 'eload1')
    """
    from lager import Net, NetType

    eload = Net.get(net, type=NetType.ELoad)
    state = {"status": "ok", "net": net}
    for attr in ("measured_voltage", "measured_current", "measured_power"):
        try:
            state[attr] = getattr(eload, attr)()
        except Exception:
            pass
    try:
        state["mode"] = eload.mode()
    except Exception:
        pass
    return json.dumps(state)


@mcp.tool()
def eload_measure(net: str, measurement: str = "current") -> str:
    """Read a single measurement from an electronic load.

    Args:
        net: Electronic load net name (e.g., 'eload1')
        measurement: What to measure — 'voltage', 'current', or 'power'
    """
    from lager import Net, NetType

    eload = Net.get(net, type=NetType.ELoad)
    method_name = f"measured_{measurement}"
    try:
        value = getattr(eload, method_name)()
    except AttributeError:
        return json.dumps({"status": "error", "error": f"Unknown measurement '{measurement}'. Use voltage, current, or power."})
    return json.dumps({"status": "ok", "net": net, "measurement": measurement, "value": value})
