# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for electronic load control."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_eload_cc(box: str, net: str, value: float = None) -> str:
    """Get or set electronic load constant current mode.

    If value is provided, sets the constant current setpoint. If omitted,
    reads and returns the current CC setting.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Electronic load net name (e.g., 'eload1')
        value: Current in amps (omit to read current setting)
    """
    if value is not None:
        return run_lager(
            "eload", net, "cc", str(value),
            "--box", box,
        )
    return run_lager("eload", net, "cc", "--box", box)


@mcp.tool()
def lager_eload_cv(box: str, net: str, value: float = None) -> str:
    """Get or set electronic load constant voltage mode.

    If value is provided, sets the constant voltage setpoint. If omitted,
    reads and returns the current CV setting.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Electronic load net name (e.g., 'eload1')
        value: Voltage in volts (omit to read current setting)
    """
    if value is not None:
        return run_lager(
            "eload", net, "cv", str(value),
            "--box", box,
        )
    return run_lager("eload", net, "cv", "--box", box)


@mcp.tool()
def lager_eload_cr(box: str, net: str, value: float = None) -> str:
    """Get or set electronic load constant resistance mode.

    If value is provided, sets the constant resistance setpoint. If omitted,
    reads and returns the current CR setting.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Electronic load net name (e.g., 'eload1')
        value: Resistance in ohms (omit to read current setting)
    """
    if value is not None:
        return run_lager(
            "eload", net, "cr", str(value),
            "--box", box,
        )
    return run_lager("eload", net, "cr", "--box", box)


@mcp.tool()
def lager_eload_cp(box: str, net: str, value: float = None) -> str:
    """Get or set electronic load constant power mode.

    If value is provided, sets the constant power setpoint. If omitted,
    reads and returns the current CP setting.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Electronic load net name (e.g., 'eload1')
        value: Power in watts (omit to read current setting)
    """
    if value is not None:
        return run_lager(
            "eload", net, "cp", str(value),
            "--box", box,
        )
    return run_lager("eload", net, "cp", "--box", box)


@mcp.tool()
def lager_eload_state(box: str, net: str) -> str:
    """Read the current state of an electronic load.

    Returns mode, setpoint, and measured values.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Electronic load net name (e.g., 'eload1')
    """
    return run_lager("eload", net, "state", "--box", box)
