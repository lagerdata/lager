# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for battery simulator control via direct on-box Net API."""

import json

from ..server import mcp


@mcp.tool()
def battery_soc(net: str, value: float = None) -> str:
    """Get or set battery state of charge.

    Args:
        net: Battery net name (e.g., 'bat1')
        value: SOC percentage to set (0-100). Omit to read current value.
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    batt.soc(value)
    if value is not None:
        return json.dumps({"status": "ok", "net": net, "soc": value})
    return json.dumps({"status": "ok", "net": net, "action": "read_soc"})


@mcp.tool()
def battery_voc(net: str, value: float = None) -> str:
    """Get or set battery open circuit voltage.

    Args:
        net: Battery net name (e.g., 'bat1')
        value: Open circuit voltage in volts. Omit to read current value.
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    batt.voc(value)
    if value is not None:
        return json.dumps({"status": "ok", "net": net, "voc": value})
    return json.dumps({"status": "ok", "net": net, "action": "read_voc"})


@mcp.tool()
def battery_enable(net: str) -> str:
    """Enable battery simulator output.

    Args:
        net: Battery net name (e.g., 'bat1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Battery).enable()
    return json.dumps({"status": "ok", "net": net, "enabled": True})


@mcp.tool()
def battery_disable(net: str) -> str:
    """Disable battery simulator output.

    Args:
        net: Battery net name (e.g., 'bat1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Battery).disable()
    return json.dumps({"status": "ok", "net": net, "enabled": False})


@mcp.tool()
def battery_state(net: str) -> str:
    """Read comprehensive battery simulator state.

    Returns terminal voltage, current, ESR, SOC, and other available readings.

    Args:
        net: Battery net name (e.g., 'bat1')
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    state = {"status": "ok", "net": net}
    for attr in ("terminal_voltage", "current", "esr"):
        try:
            state[attr] = getattr(batt, attr)()
        except Exception:
            pass
    return json.dumps(state)


@mcp.tool()
def battery_clear_ocp(net: str) -> str:
    """Clear an over-current protection fault on a battery simulator.

    Args:
        net: Battery net name (e.g., 'bat1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Battery).clear_ocp()
    return json.dumps({"status": "ok", "net": net, "cleared": "ocp"})


@mcp.tool()
def battery_clear_ovp(net: str) -> str:
    """Clear an over-voltage protection fault on a battery simulator.

    Args:
        net: Battery net name (e.g., 'bat1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Battery).clear_ovp()
    return json.dumps({"status": "ok", "net": net, "cleared": "ovp"})


@mcp.tool()
def battery_current_limit(net: str, value: float = None) -> str:
    """Get or set battery simulator current limit.

    Args:
        net: Battery net name (e.g., 'bat1')
        value: Current limit in amps. Omit to read current setting.
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    batt.current_limit(value)
    if value is not None:
        return json.dumps({"status": "ok", "net": net, "current_limit": value})
    return json.dumps({"status": "ok", "net": net, "action": "read_current_limit"})


@mcp.tool()
def battery_mode(net: str, mode_type: str = None) -> str:
    """Get or set battery simulator mode.

    Args:
        net: Battery net name (e.g., 'bat1')
        mode_type: Mode to set — 'static' or 'dynamic'. Omit to read current mode.
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    batt.mode(mode_type)
    if mode_type is not None:
        return json.dumps({"status": "ok", "net": net, "mode": mode_type})
    return json.dumps({"status": "ok", "net": net, "action": "read_mode"})


@mcp.tool()
def battery_set(net: str) -> str:
    """Apply current battery simulator configuration to hardware.

    Puts the instrument into battery simulation mode with the previously
    configured parameters.

    Args:
        net: Battery net name (e.g., 'bat1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Battery).set_mode_battery()
    return json.dumps({"status": "ok", "net": net, "action": "set_mode_battery"})


@mcp.tool()
def battery_voltage_full(net: str, voltage: float = None) -> str:
    """Get or set the fully-charged battery voltage.

    Args:
        net: Battery net name (e.g., 'bat1')
        voltage: Full battery voltage in volts. Omit to read current value.
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    batt.voltage_full(voltage)
    if voltage is not None:
        return json.dumps({"status": "ok", "net": net, "voltage_full": voltage})
    return json.dumps({"status": "ok", "net": net, "action": "read_voltage_full"})


@mcp.tool()
def battery_voltage_empty(net: str, voltage: float = None) -> str:
    """Get or set the fully-discharged battery voltage.

    Args:
        net: Battery net name (e.g., 'bat1')
        voltage: Empty battery voltage in volts. Omit to read current value.
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    batt.voltage_empty(voltage)
    if voltage is not None:
        return json.dumps({"status": "ok", "net": net, "voltage_empty": voltage})
    return json.dumps({"status": "ok", "net": net, "action": "read_voltage_empty"})


@mcp.tool()
def battery_capacity(net: str, amp_hours: float = None) -> str:
    """Get or set battery simulator capacity.

    Args:
        net: Battery net name (e.g., 'bat1')
        amp_hours: Battery capacity in amp-hours. Omit to read current value.
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    batt.capacity(amp_hours)
    if amp_hours is not None:
        return json.dumps({"status": "ok", "net": net, "capacity_ah": amp_hours})
    return json.dumps({"status": "ok", "net": net, "action": "read_capacity"})


@mcp.tool()
def battery_ovp(net: str, voltage: float = None) -> str:
    """Get or set battery over-voltage protection threshold.

    Args:
        net: Battery net name (e.g., 'bat1')
        voltage: OVP threshold in volts. Omit to read current value.
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    batt.ovp(voltage)
    if voltage is not None:
        return json.dumps({"status": "ok", "net": net, "ovp": voltage})
    return json.dumps({"status": "ok", "net": net, "action": "read_ovp"})


@mcp.tool()
def battery_ocp(net: str, current: float = None) -> str:
    """Get or set battery over-current protection threshold.

    Args:
        net: Battery net name (e.g., 'bat1')
        current: OCP threshold in amps. Omit to read current value.
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    batt.ocp(current)
    if current is not None:
        return json.dumps({"status": "ok", "net": net, "ocp": current})
    return json.dumps({"status": "ok", "net": net, "action": "read_ocp"})


@mcp.tool()
def battery_model(net: str, partnumber: str = None) -> str:
    """Get or set the battery model / part number.

    Battery models define voltage-SOC discharge curves and internal resistance.

    Args:
        net: Battery net name (e.g., 'bat1')
        partnumber: Battery model identifier. Omit to read current model.
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    batt.model(partnumber)
    if partnumber is not None:
        return json.dumps({"status": "ok", "net": net, "model": partnumber})
    return json.dumps({"status": "ok", "net": net, "action": "read_model"})


@mcp.tool()
def battery_clear(net: str) -> str:
    """Clear all battery simulator faults and reset protection state.

    Args:
        net: Battery net name (e.g., 'bat1')
    """
    from lager import Net, NetType

    batt = Net.get(net, type=NetType.Battery)
    batt.clear_ocp()
    batt.clear_ovp()
    return json.dumps({"status": "ok", "net": net, "cleared": "all"})
