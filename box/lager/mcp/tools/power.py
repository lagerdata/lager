# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for power supply control via direct on-box Net API."""

import json

from ..server import mcp


@mcp.tool()
def supply_set_voltage(net: str, voltage: float) -> str:
    """Set power supply voltage.

    Args:
        net: Power supply net name (e.g., 'psu1')
        voltage: Voltage in volts
    """
    from lager import Net, NetType

    supply = Net.get(net, type=NetType.PowerSupply)
    supply.set_voltage(voltage)
    return json.dumps({"status": "ok", "net": net, "voltage": voltage})


@mcp.tool()
def supply_set_current(net: str, current: float) -> str:
    """Set power supply current limit.

    Args:
        net: Power supply net name (e.g., 'psu1')
        current: Current limit in amps
    """
    from lager import Net, NetType

    supply = Net.get(net, type=NetType.PowerSupply)
    supply.set_current(current)
    return json.dumps({"status": "ok", "net": net, "current": current})


@mcp.tool()
def supply_enable(net: str) -> str:
    """Enable power supply output.

    Args:
        net: Power supply net name (e.g., 'psu1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.PowerSupply).enable()
    return json.dumps({"status": "ok", "net": net, "enabled": True})


@mcp.tool()
def supply_disable(net: str) -> str:
    """Disable power supply output.

    Args:
        net: Power supply net name (e.g., 'psu1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.PowerSupply).disable()
    return json.dumps({"status": "ok", "net": net, "enabled": False})


@mcp.tool()
def supply_measure(net: str, measurement: str = "voltage") -> str:
    """Read a measurement from a power supply net.

    Args:
        net: Power supply net name (e.g., 'psu1')
        measurement: What to measure — 'voltage', 'current', or 'power'
    """
    from lager import Net, NetType

    supply = Net.get(net, type=NetType.PowerSupply)
    value = getattr(supply, measurement)()
    return json.dumps({"status": "ok", "net": net, "measurement": measurement, "value": value})


@mcp.tool()
def supply_state(net: str) -> str:
    """Read the full state of a power supply (voltage, current, enabled).

    Args:
        net: Power supply net name (e.g., 'psu1')
    """
    from lager import Net, NetType

    supply = Net.get(net, type=NetType.PowerSupply)
    state = {
        "status": "ok",
        "net": net,
        "voltage": supply.voltage(),
        "current": supply.current(),
    }
    try:
        state["power"] = supply.power()
    except Exception:
        pass
    try:
        state["enabled"] = supply.output_is_enabled()
    except Exception:
        pass
    return json.dumps(state)


@mcp.tool()
def supply_ocp(net: str, value: float = None) -> str:
    """Get or set over-current protection limit.

    Args:
        net: Power supply net name (e.g., 'psu1')
        value: OCP limit in amps. Omit to read current setting.
    """
    from lager import Net, NetType

    supply = Net.get(net, type=NetType.PowerSupply)
    supply.ocp(value)
    if value is not None:
        return json.dumps({"status": "ok", "net": net, "ocp": value})
    return json.dumps({"status": "ok", "net": net, "action": "read_ocp"})


@mcp.tool()
def supply_ovp(net: str, value: float = None) -> str:
    """Get or set over-voltage protection limit.

    Args:
        net: Power supply net name (e.g., 'psu1')
        value: OVP limit in volts. Omit to read current setting.
    """
    from lager import Net, NetType

    supply = Net.get(net, type=NetType.PowerSupply)
    supply.ovp(value)
    if value is not None:
        return json.dumps({"status": "ok", "net": net, "ovp": value})
    return json.dumps({"status": "ok", "net": net, "action": "read_ovp"})


@mcp.tool()
def supply_clear_ocp(net: str) -> str:
    """Clear an over-current protection trip on a power supply.

    Resets the OCP latch so the supply can be re-enabled after a current fault.

    Args:
        net: Power supply net name (e.g., 'psu1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.PowerSupply).clear_ocp()
    return json.dumps({"status": "ok", "net": net, "cleared": "ocp"})


@mcp.tool()
def supply_clear_ovp(net: str) -> str:
    """Clear an over-voltage protection trip on a power supply.

    Resets the OVP latch so the supply can be re-enabled after a voltage fault.

    Args:
        net: Power supply net name (e.g., 'psu1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.PowerSupply).clear_ovp()
    return json.dumps({"status": "ok", "net": net, "cleared": "ovp"})


@mcp.tool()
def supply_set_mode(net: str) -> str:
    """Set the instrument to DC power supply mode.

    Useful when the instrument supports multiple modes (e.g., battery + supply).

    Args:
        net: Power supply net name (e.g., 'psu1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.PowerSupply).set_mode()
    return json.dumps({"status": "ok", "net": net, "action": "set_mode"})
