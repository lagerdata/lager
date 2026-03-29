# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for measurement instruments (ADC, DAC, GPIO)."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_adc_read(box: str, net: str) -> str:
    """Read voltage from an ADC (analog-to-digital converter) channel.

    Returns the measured voltage on the specified ADC net.

    Args:
        box: Box name (e.g., 'DEMO')
        net: ADC net name (e.g., 'adc1')
    """
    return run_lager("adc", net, "--box", box)


@mcp.tool()
def lager_dac_write(box: str, net: str, voltage: float = None) -> str:
    """Set or read DAC (digital-to-analog converter) output voltage.

    If voltage is provided, sets the DAC output. If omitted, reads the
    current DAC value. Valid range: 0-10V (depends on hardware).

    Args:
        box: Box name (e.g., 'DEMO')
        net: DAC net name (e.g., 'dac1')
        voltage: Output voltage in volts (omit to read current value)
    """
    if voltage is not None:
        return run_lager("dac", net, str(voltage), "--box", box)
    return run_lager("dac", net, "--box", box)


@mcp.tool()
def lager_gpi_read(box: str, net: str) -> str:
    """Read the state of a GPIO input pin.

    Returns 0 (low) or 1 (high).

    Args:
        box: Box name (e.g., 'DEMO')
        net: GPIO net name (e.g., 'gpio1')
    """
    return run_lager("gpi", net, "--box", box)


@mcp.tool()
def lager_gpo_set(box: str, net: str, level: str, hold: bool = False) -> str:
    """Set a GPIO output pin to a specified level.

    Args:
        box: Box name (e.g., 'DEMO')
        net: GPIO net name (e.g., 'gpio1')
        level: Output level - 'high', 'low', 'on', 'off', '1', '0', or 'toggle'
        hold: Keep the output level after the command exits (default: false)
    """
    args = ["gpo", net, level, "--box", box]
    if hold:
        args.insert(3, "--hold")
    return run_lager(*args)


@mcp.tool()
def lager_thermocouple_read(box: str, net: str) -> str:
    """Read temperature from a thermocouple in degrees Celsius.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Thermocouple net name (e.g., 'tc1')
    """
    return run_lager("thermocouple", net, "--box", box)


@mcp.tool()
def lager_watt_read(box: str, net: str) -> str:
    """Read power from a watt meter in watts.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Watt meter net name (e.g., 'watt1')
    """
    return run_lager("watt", net, "--box", box)


@mcp.tool()
def lager_gpi_wait_for(
    box: str, net: str, level: str, timeout: float = 30.0,
) -> str:
    """Wait for a GPIO input pin to reach a specified level.

    Blocks until the pin reaches the target level or timeout expires.

    Args:
        box: Box name (e.g., 'DEMO')
        net: GPIO net name (e.g., 'gpio1')
        level: Target level - 'high', 'low', '1', or '0'
        timeout: Maximum seconds to wait (default: 30)
    """
    return run_lager(
        "gpi", net, "--wait-for", level, "--timeout", str(timeout), "--box", box,
    )
