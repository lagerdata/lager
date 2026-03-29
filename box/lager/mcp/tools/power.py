# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for power supply control."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_supply_voltage(
    box: str, net: str, voltage: float = None,
    ocp: float = None, ovp: float = None,
) -> str:
    """Get or set power supply voltage.

    If voltage is provided, sets the output voltage (with --yes to skip
    confirmation). If omitted, reads and returns the current voltage.
    OCP/OVP protection limits can be set at the same time as voltage.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Power supply net name (e.g., 'psu1')
        voltage: Voltage to set in volts (omit to read current voltage)
        ocp: Over-current protection limit in amps (optional)
        ovp: Over-voltage protection limit in volts (optional)
    """
    if voltage is not None:
        args = ["supply", net, "voltage", str(voltage), "--yes", "--box", box]
        if ocp is not None:
            args.extend(["--ocp", str(ocp)])
        if ovp is not None:
            args.extend(["--ovp", str(ovp)])
        return run_lager(*args)
    return run_lager("supply", net, "voltage", "--box", box)


@mcp.tool()
def lager_supply_current(box: str, net: str, current: float = None) -> str:
    """Get or set power supply current limit.

    If current is provided, sets the current limit (with --yes to skip
    confirmation). If omitted, reads and returns the current setting.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Power supply net name (e.g., 'psu1')
        current: Current limit in amps (omit to read current setting)
    """
    if current is not None:
        return run_lager(
            "supply", net, "current", str(current),
            "--yes", "--box", box,
        )
    return run_lager("supply", net, "current", "--box", box)


@mcp.tool()
def lager_supply_enable(box: str, net: str) -> str:
    """Enable power supply output.

    Turns on the power supply output at the previously configured
    voltage and current settings.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Power supply net name (e.g., 'psu1')
    """
    return run_lager("supply", net, "enable", "--yes", "--box", box)


@mcp.tool()
def lager_supply_disable(box: str, net: str) -> str:
    """Disable power supply output.

    Turns off the power supply output. Voltage and current settings
    are preserved for when output is re-enabled.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Power supply net name (e.g., 'psu1')
    """
    return run_lager("supply", net, "disable", "--yes", "--box", box)


@mcp.tool()
def lager_supply_state(box: str, net: str) -> str:
    """Read the current state of a power supply.

    Returns voltage, current, and output enable status.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Power supply net name (e.g., 'psu1')
    """
    return run_lager("supply", net, "state", "--box", box)


@mcp.tool()
def lager_supply_clear_ocp(box: str, net: str) -> str:
    """Clear an over-current protection fault on a power supply.

    Resets the OCP latch so the supply can be re-enabled after a
    current fault event.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Power supply net name (e.g., 'psu1')
    """
    return run_lager("supply", net, "clear-ocp", "--box", box)


@mcp.tool()
def lager_supply_clear_ovp(box: str, net: str) -> str:
    """Clear an over-voltage protection fault on a power supply.

    Resets the OVP latch so the supply can be re-enabled after a
    voltage fault event.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Power supply net name (e.g., 'psu1')
    """
    return run_lager("supply", net, "clear-ovp", "--box", box)


@mcp.tool()
def lager_supply_set(box: str, net: str) -> str:
    """Apply the current power supply configuration.

    Sends the previously configured voltage and current settings
    to the power supply hardware.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Power supply net name (e.g., 'psu1')
    """
    return run_lager("supply", net, "set", "--box", box)
