# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for battery simulator control."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_battery_soc(box: str, net: str, value: float = None) -> str:
    """Get or set battery state of charge.

    If value is provided, sets the SOC percentage. If omitted, reads
    and returns the current SOC.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
        value: SOC percentage to set (omit to read current value)
    """
    if value is not None:
        return run_lager(
            "battery", net, "soc", str(value),
            "--box", box,
        )
    return run_lager("battery", net, "soc", "--box", box)


@mcp.tool()
def lager_battery_voc(box: str, net: str, value: float = None) -> str:
    """Get or set battery open circuit voltage.

    If value is provided, sets the open circuit voltage. If omitted,
    reads and returns the current VOC.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
        value: Open circuit voltage in volts (omit to read current value)
    """
    if value is not None:
        return run_lager(
            "battery", net, "voc", str(value),
            "--box", box,
        )
    return run_lager("battery", net, "voc", "--box", box)


@mcp.tool()
def lager_battery_enable(box: str, net: str) -> str:
    """Enable battery simulator output.

    Turns on the battery simulator at the previously configured settings.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
    """
    return run_lager("battery", net, "enable", "--yes", "--box", box)


@mcp.tool()
def lager_battery_disable(box: str, net: str) -> str:
    """Disable battery simulator output.

    Turns off the battery simulator output. Settings are preserved
    for when output is re-enabled.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
    """
    return run_lager("battery", net, "disable", "--yes", "--box", box)


@mcp.tool()
def lager_battery_state(box: str, net: str) -> str:
    """Read the current state of a battery simulator.

    Returns SOC, VOC, current, and output enable status.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
    """
    return run_lager("battery", net, "state", "--box", box)


@mcp.tool()
def lager_battery_clear_ocp(box: str, net: str) -> str:
    """Clear an over-current protection fault on a battery simulator.

    Resets the OCP latch so the simulator can be re-enabled after a
    current fault event.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
    """
    return run_lager("battery", net, "clear-ocp", "--box", box)


@mcp.tool()
def lager_battery_clear_ovp(box: str, net: str) -> str:
    """Clear an over-voltage protection fault on a battery simulator.

    Resets the OVP latch so the simulator can be re-enabled after a
    voltage fault event.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
    """
    return run_lager("battery", net, "clear-ovp", "--box", box)


@mcp.tool()
def lager_battery_current_limit(box: str, net: str, value: float = None) -> str:
    """Get or set battery simulator current limit.

    If value is provided, sets the current limit. If omitted, reads
    and returns the current limit setting.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
        value: Current limit in amps (omit to read current setting)
    """
    if value is not None:
        return run_lager(
            "battery", net, "current-limit", str(value),
            "--box", box,
        )
    return run_lager("battery", net, "current-limit", "--box", box)


@mcp.tool()
def lager_battery_mode(box: str, net: str, mode_type: str = None) -> str:
    """Get or set battery simulator mode.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
        mode_type: Mode to set ('static' or 'dynamic'). Omit to read current mode.
    """
    if mode_type is not None:
        return run_lager("battery", net, "mode", mode_type, "--box", box)
    return run_lager("battery", net, "mode", "--box", box)


@mcp.tool()
def lager_battery_set(box: str, net: str) -> str:
    """Apply the current battery simulator configuration.

    Sends the previously configured settings to the battery
    simulator hardware.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
    """
    return run_lager("battery", net, "set", "--box", box)


@mcp.tool()
def lager_battery_batt_full(box: str, net: str, voltage: float = None) -> str:
    """Get or set the full battery voltage.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
        voltage: Full battery voltage in volts (omit to read current value)
    """
    if voltage is not None:
        return run_lager(
            "battery", net, "batt-full", str(voltage),
            "--box", box,
        )
    return run_lager("battery", net, "batt-full", "--box", box)


@mcp.tool()
def lager_battery_batt_empty(box: str, net: str, voltage: float = None) -> str:
    """Get or set the empty battery voltage.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
        voltage: Empty battery voltage in volts (omit to read current value)
    """
    if voltage is not None:
        return run_lager(
            "battery", net, "batt-empty", str(voltage),
            "--box", box,
        )
    return run_lager("battery", net, "batt-empty", "--box", box)


@mcp.tool()
def lager_battery_capacity(box: str, net: str, amp_hours: float = None) -> str:
    """Get or set battery simulator capacity.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
        amp_hours: Battery capacity in amp-hours (omit to read current value)
    """
    if amp_hours is not None:
        return run_lager(
            "battery", net, "capacity", str(amp_hours),
            "--box", box,
        )
    return run_lager("battery", net, "capacity", "--box", box)


@mcp.tool()
def lager_battery_ovp(box: str, net: str, voltage: float = None) -> str:
    """Get or set battery over-voltage protection threshold.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
        voltage: OVP threshold in volts (omit to read current value)
    """
    if voltage is not None:
        return run_lager(
            "battery", net, "ovp", str(voltage),
            "--box", box,
        )
    return run_lager("battery", net, "ovp", "--box", box)


@mcp.tool()
def lager_battery_ocp(box: str, net: str, current: float = None) -> str:
    """Get or set battery over-current protection threshold.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
        current: OCP threshold in amps (omit to read current value)
    """
    if current is not None:
        return run_lager(
            "battery", net, "ocp", str(current),
            "--box", box,
        )
    return run_lager("battery", net, "ocp", "--box", box)


@mcp.tool()
def lager_battery_model(box: str, net: str, partnumber: str = None) -> str:
    """Get or set battery model part number.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
        partnumber: Battery model part number (omit to read current value)
    """
    if partnumber is not None:
        return run_lager(
            "battery", net, "model", partnumber,
            "--box", box,
        )
    return run_lager("battery", net, "model", "--box", box)


@mcp.tool()
def lager_battery_clear(box: str, net: str) -> str:
    """Clear all battery simulator faults and reset state.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Battery net name (e.g., 'bat1')
    """
    return run_lager("battery", net, "clear", "--box", box)
