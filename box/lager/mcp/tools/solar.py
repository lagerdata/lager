# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for solar simulator control."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_solar_set(box: str, net: str) -> str:
    """Apply the current solar simulator configuration.

    Sends the previously configured irradiance, temperature, and
    resistance values to the solar simulator hardware.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Solar net name (e.g., 'solar1')
    """
    return run_lager("solar", net, "set", "--box", box)


@mcp.tool()
def lager_solar_stop(box: str, net: str) -> str:
    """Stop the solar simulator output.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Solar net name (e.g., 'solar1')
    """
    return run_lager("solar", net, "stop", "--box", box)


@mcp.tool()
def lager_solar_irradiance(box: str, net: str, value: float = None) -> str:
    """Get or set solar simulator irradiance.

    If value is provided, sets the irradiance level. If omitted,
    reads and returns the current irradiance.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Solar net name (e.g., 'solar1')
        value: Irradiance value to set (omit to read current value)
    """
    if value is not None:
        return run_lager(
            "solar", net, "irradiance", str(value),
            "--box", box,
        )
    return run_lager("solar", net, "irradiance", "--box", box)


@mcp.tool()
def lager_solar_mpp_current(box: str, net: str) -> str:
    """Read the maximum power point current from the solar simulator.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Solar net name (e.g., 'solar1')
    """
    return run_lager("solar", net, "mpp-current", "--box", box)


@mcp.tool()
def lager_solar_mpp_voltage(box: str, net: str) -> str:
    """Read the maximum power point voltage from the solar simulator.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Solar net name (e.g., 'solar1')
    """
    return run_lager("solar", net, "mpp-voltage", "--box", box)


@mcp.tool()
def lager_solar_resistance(box: str, net: str, value: float = None) -> str:
    """Get or set solar simulator series resistance.

    If value is provided, sets the resistance. If omitted,
    reads and returns the current resistance.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Solar net name (e.g., 'solar1')
        value: Resistance value to set (omit to read current value)
    """
    if value is not None:
        return run_lager(
            "solar", net, "resistance", str(value),
            "--box", box,
        )
    return run_lager("solar", net, "resistance", "--box", box)


@mcp.tool()
def lager_solar_temperature(box: str, net: str) -> str:
    """Read the solar simulator temperature.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Solar net name (e.g., 'solar1')
    """
    return run_lager("solar", net, "temperature", "--box", box)


@mcp.tool()
def lager_solar_voc(box: str, net: str, value: float = None) -> str:
    """Get or set solar simulator open circuit voltage.

    If value is provided, sets the VOC. If omitted,
    reads and returns the current VOC.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Solar net name (e.g., 'solar1')
        value: Open circuit voltage to set (omit to read current value)
    """
    if value is not None:
        return run_lager(
            "solar", net, "voc", str(value),
            "--box", box,
        )
    return run_lager("solar", net, "voc", "--box", box)
