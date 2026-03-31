# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for solar simulator control via direct on-box dispatcher.

There is no NetType.Solar — the solar dispatcher resolves nets with
role='solar' from saved_nets.json and instantiates the appropriate
driver (EA PSB series) directly.
"""

import json

from ..server import mcp


def _get_driver(net: str):
    """Resolve a solar net name to a connected driver instance."""
    from lager.power.solar.dispatcher import SolarDispatcher
    dispatcher = SolarDispatcher()
    drv = dispatcher.resolve_driver(net)
    drv.connect_instrument()
    return drv


@mcp.tool()
def solar_set(net: str) -> str:
    """Initialize and start the solar simulation mode.

    Puts the instrument into PV simulation mode with the previously
    configured parameters.

    Args:
        net: Solar net name (e.g., 'solar1')
    """
    _get_driver(net)
    return json.dumps({"status": "ok", "net": net, "action": "set_solar_mode"})


@mcp.tool()
def solar_stop(net: str) -> str:
    """Stop the solar simulator output.

    Args:
        net: Solar net name (e.g., 'solar1')
    """
    from lager.power.solar.dispatcher import SolarDispatcher
    dispatcher = SolarDispatcher()
    drv = dispatcher.resolve_driver(net)
    try:
        drv.connect_instrument()
    except Exception:
        pass
    drv.disconnect_instrument()
    return json.dumps({"status": "ok", "net": net, "action": "stop"})


@mcp.tool()
def solar_irradiance(net: str, value: float = None) -> str:
    """Get or set solar simulator irradiance (W/m^2).

    Args:
        net: Solar net name (e.g., 'solar1')
        value: Irradiance to set (0-1500 W/m^2). Omit to read current value.
    """
    drv = _get_driver(net)
    result = drv.irradiance(value=value)
    resp = {"status": "ok", "net": net}
    if value is not None:
        resp["irradiance"] = value
    elif result is not None:
        resp["irradiance"] = result.strip() if isinstance(result, str) else result
    return json.dumps(resp)


@mcp.tool()
def solar_mpp_current(net: str) -> str:
    """Read the maximum power point current from the solar simulator.

    Args:
        net: Solar net name (e.g., 'solar1')
    """
    result = _get_driver(net).mpp_current()
    return json.dumps({"status": "ok", "net": net, "mpp_current": result})


@mcp.tool()
def solar_mpp_voltage(net: str) -> str:
    """Read the maximum power point voltage from the solar simulator.

    Args:
        net: Solar net name (e.g., 'solar1')
    """
    result = _get_driver(net).mpp_voltage()
    return json.dumps({"status": "ok", "net": net, "mpp_voltage": result})


@mcp.tool()
def solar_resistance(net: str, value: float = None) -> str:
    """Get or set solar simulator series resistance (ohms).

    Args:
        net: Solar net name (e.g., 'solar1')
        value: Resistance to set. Omit to read current value.
    """
    drv = _get_driver(net)
    result = drv.resistance(value=value) if value is None else drv.resistance(value)
    resp = {"status": "ok", "net": net}
    if value is not None:
        resp["resistance"] = value
    elif result is not None:
        resp["resistance"] = result.strip() if isinstance(result, str) else result
    return json.dumps(resp)


@mcp.tool()
def solar_temperature(net: str) -> str:
    """Read the solar simulator cell temperature.

    Args:
        net: Solar net name (e.g., 'solar1')
    """
    result = _get_driver(net).temperature()
    return json.dumps({"status": "ok", "net": net, "temperature": result})


@mcp.tool()
def solar_voc(net: str) -> str:
    """Read the solar simulator open circuit voltage (Voc).

    Args:
        net: Solar net name (e.g., 'solar1')
    """
    result = _get_driver(net).voc()
    return json.dumps({"status": "ok", "net": net, "voc": result})
