# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Consolidated helper functions for net-based CLI commands.

This module provides common functionality for commands that work with nets,
eliminating code duplication across supply, battery, solar, eload, scope,
logic, thermocouple, watt, and other net-based command modules.

Usage:
    from cli.core.net_helpers import (
        require_netname,
        resolve_box,
        run_net_py,
        list_nets_by_role,
        validate_net,
        display_nets,
        display_nets_table,
        run_backend,
        run_backend_with_env,
        run_impl_script,
    )

Example:
    @my_command.command()
    @click.pass_context
    @click.option("--box", required=False)
    def voltage(ctx, box):
        box_ip = resolve_box(ctx, box)
        netname = require_netname(ctx, "supply")
        run_backend(ctx, box_ip, "supply.py", "voltage", netname=netname)
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from typing import TYPE_CHECKING

import click
from texttable import Texttable

if TYPE_CHECKING:
    from typing import Any, Callable


# =============================================================================
# Box Resolution
# =============================================================================

def resolve_box(ctx: click.Context, box: str | None) -> str:
    """
    Resolve box name to IP address if it's a local box.

    This function handles the common pattern of resolving a box name or IP
    address to a validated box IP. It uses the box_storage module to look up
    local box configurations.

    Args:
        ctx: Click context object
        box: Box name (from local config) or IP address, or None for default

    Returns:
        Resolved and validated box IP address

    Raises:
        click.UsageError: If box cannot be resolved

    Example:
        box_ip = resolve_box(ctx, box)
    """
    from ..box_storage import resolve_and_validate_box
    return resolve_and_validate_box(ctx, box)


# =============================================================================
# Netname Handling
# =============================================================================

def require_netname(ctx: click.Context, command_name: str) -> str:
    """
    Get the netname from context, raising a UsageError if not set.

    This function implements the common pattern of requiring a net name
    argument for commands that operate on specific nets.

    Args:
        ctx: Click context object (must have ctx.obj.netname set by group)
        command_name: Name of the command for error message (e.g., "supply", "scope")

    Returns:
        The netname string

    Raises:
        click.UsageError: If netname is not set in context

    Example:
        @supply.command()
        @click.pass_context
        def voltage(ctx, box):
            netname = require_netname(ctx, "supply")
            # netname is now guaranteed to be a non-empty string
    """
    netname = getattr(ctx.obj, "netname", None)
    if not netname:
        raise click.UsageError(
            f"NETNAME required.\n\n"
            f"Usage: lager {command_name} <NETNAME> <COMMAND>\n"
            f"Example: lager {command_name} {command_name}1 disable"
        )
    return netname


def get_netname_or_none(ctx: click.Context) -> str | None:
    """
    Get the netname from context, returning None if not set.

    Unlike require_netname(), this function does not raise an error
    if netname is not set. Useful for commands that can operate
    without a specific net (e.g., listing all nets).

    Args:
        ctx: Click context object

    Returns:
        The netname string if set, None otherwise
    """
    return getattr(ctx.obj, "netname", None)


# =============================================================================
# Net Operations (querying box for nets)
# =============================================================================

def run_net_py(ctx: click.Context, box: str, *args: str) -> list[dict]:
    """
    Run the net.py implementation script and return parsed JSON results.

    This function executes net.py on the box to retrieve net configurations.
    It captures stdout and parses the JSON output.

    Args:
        ctx: Click context object
        box: Box IP address
        *args: Arguments to pass to net.py (default: "list")

    Returns:
        List of net dictionaries parsed from JSON output.
        Returns empty list if parsing fails or no nets found.

    Example:
        nets = run_net_py(ctx, box_ip, "list")
        for net in nets:
            print(f"Net: {net['name']}, Role: {net['role']}")
    """
    from ..context import get_impl_path
    from ..commands.development.python import run_python_internal

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            run_python_internal(
                ctx,
                get_impl_path("net.py"),
                box,
                env={},
                passenv=(),
                kill=False,
                download=(),
                allow_overwrite=False,
                signum="SIGTERM",
                timeout=0,
                detach=False,
                port=(),
                org=None,
                args=args or ("list",),
            )
    except SystemExit:
        pass
    raw = buf.getvalue() or "[]"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def list_nets_by_role(ctx: click.Context, box: str, role: str) -> list[dict]:
    """
    List all nets with the specified role.

    This function filters the nets returned by run_net_py() to only
    include nets matching the specified role.

    Args:
        ctx: Click context object
        box: Box IP address
        role: Net role to filter by (e.g., "power-supply", "battery", "scope")

    Returns:
        List of net dictionaries matching the specified role

    Example:
        supply_nets = list_nets_by_role(ctx, box_ip, "power-supply")
        battery_nets = list_nets_by_role(ctx, box_ip, "battery")
    """
    recs = run_net_py(ctx, box, "list")
    return [r for r in recs if r.get("role") == role]


def validate_net(ctx: click.Context, box: str, netname: str, net_role: str) -> bool:
    """
    Validate that a net exists and has the specified role.

    This function checks if a named net exists on the box and has
    the expected role. Useful for validating user input before
    executing commands.

    Args:
        ctx: Click context object
        box: Box IP address
        netname: Name of the net to validate
        net_role: Expected role of the net

    Returns:
        True if net exists with the specified role, False otherwise

    Example:
        if not validate_net(ctx, box_ip, netname, "power-supply"):
            click.echo(f"{netname} is not a power supply net")
            return
    """
    nets = run_net_py(ctx, box, "list")
    for net in nets:
        if net.get("name") == netname and net.get("role") == net_role:
            return True
    return False


def find_net_by_name(ctx: click.Context, box: str, netname: str) -> dict | None:
    """
    Find a net by name and return its full record.

    Args:
        ctx: Click context object
        box: Box IP address
        netname: Name of the net to find

    Returns:
        Net dictionary if found, None otherwise

    Example:
        net = find_net_by_name(ctx, box_ip, "supply1")
        if net:
            print(f"Instrument: {net.get('instrument')}")
    """
    nets = run_net_py(ctx, box, "list")
    for net in nets:
        if net.get("name") == netname:
            return net
    return None


# =============================================================================
# Display Functions
# =============================================================================

def display_nets(
    ctx: click.Context,
    box: str,
    netname: str | None,
    role: str,
    role_label: str,
) -> None:
    """
    Display nets of a specific role in a formatted table.

    This function fetches nets matching the specified role and displays
    them in a formatted ASCII table using texttable.

    Args:
        ctx: Click context object
        box: Box IP address
        netname: Optional specific net name to display, or None for all
        role: Net role to filter by (e.g., "power-supply", "battery")
        role_label: Human-readable label for the role (e.g., "power supply", "battery")

    Example:
        # Display all power supply nets
        display_nets(ctx, box_ip, None, "power-supply", "power supply")

        # Display a specific net
        display_nets(ctx, box_ip, "supply1", "power-supply", "power supply")
    """
    nets = list_nets_by_role(ctx, box, role)
    if not nets:
        click.echo(f"No {role_label} nets found on this box.")
        return

    table = Texttable()
    table.set_deco(Texttable.HEADER)
    table.set_cols_dtype(["t", "t", "t", "t", "t"])
    table.set_cols_align(["l", "l", "l", "l", "l"])
    table.header(["Name", "Net Type", "Instrument", "Channel", "Address"])

    for rec in nets:
        if netname is None or netname == rec.get("name"):
            table.add_row([
                rec.get("name", ""),
                rec.get("role", ""),
                rec.get("instrument", ""),
                rec.get("pin", ""),
                rec.get("address", "")
            ])

    click.echo(table.draw())


def display_nets_table(
    nets: list[dict],
    headers: list[str] | None = None,
    empty_message: str = "No nets to display.",
) -> None:
    """
    Display a list of net records in a formatted table.

    This is a simpler alternative to display_nets() that takes pre-fetched
    net records directly, useful when you've already filtered/processed the data.

    Args:
        nets: List of net dictionaries with keys: name, role, instrument, pin, address
        headers: Optional list of column headers (default: standard net columns)
        empty_message: Message to show if nets list is empty

    Example:
        nets = list_nets_by_role(ctx, box, "power-supply")
        display_nets_table(nets, empty_message="No power supplies configured")
    """
    if not nets:
        click.echo(empty_message)
        return

    if headers is None:
        headers = ["Name", "Net Type", "Instrument", "Channel", "Address"]

    table = Texttable()
    table.set_deco(Texttable.HEADER)
    table.set_cols_dtype(["t"] * len(headers))
    table.set_cols_align(["l"] * len(headers))
    table.header(headers)

    for rec in nets:
        table.add_row([
            rec.get("name", ""),
            rec.get("role", ""),
            rec.get("instrument", ""),
            rec.get("pin", ""),
            rec.get("address", "")
        ])

    click.echo(table.draw())


# =============================================================================
# Backend Execution Functions
# =============================================================================

def run_backend(
    ctx: click.Context,
    box: str,
    impl_script: str,
    action: str,
    **params: Any,
) -> None:
    """
    Run a backend implementation script with the given action and parameters.

    This function constructs the command data JSON and executes the
    implementation script on the box. It uses the LAGER_COMMAND_DATA
    environment variable to pass action and parameters.

    Args:
        ctx: Click context object
        box: Box IP address
        impl_script: Name of the implementation script (e.g., "scope.py", "supply.py")
        action: Action name to pass to the script
        **params: Additional parameters to pass to the script

    Example:
        run_backend(ctx, box_ip, "scope.py", "enable_net", netname="scope1", mcu=None)
        run_backend(ctx, box_ip, "supply.py", "voltage", netname="supply1", value=3.3)
    """
    from ..context import get_impl_path
    from ..commands.development.python import run_python_internal

    # Extract mcu from params if present (common pattern)
    mcu = params.pop("mcu", None)

    data = {
        "action": action,
        "mcu": mcu,
        "params": params,
    }

    run_python_internal(
        ctx,
        get_impl_path(impl_script),
        box,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


def run_backend_with_env(
    ctx: click.Context,
    box: str,
    impl_script: str,
    action: str,
    extra_env: tuple[str, ...] = (),
    timeout: int = 0,
    **params: Any,
) -> None:
    """
    Run a backend script with additional environment variables and options.

    Extended version of run_backend() that supports additional environment
    variables and custom timeout. Useful for commands that need extra
    configuration or longer execution times.

    Args:
        ctx: Click context object
        box: Box IP address
        impl_script: Name of the implementation script
        action: Action name to pass to the script
        extra_env: Additional environment variables as tuple of "KEY=value" strings
        timeout: Custom timeout in seconds (0 for no timeout)
        **params: Additional parameters to pass to the script

    Example:
        run_backend_with_env(
            ctx, box_ip, "debug_flash.py", "flash",
            extra_env=("DEBUG_LEVEL=2",),
            timeout=120,
            netname="debug1",
            hexfile="/path/to/firmware.hex"
        )
    """
    from ..context import get_impl_path
    from ..commands.development.python import run_python_internal

    mcu = params.pop("mcu", None)

    data = {
        "action": action,
        "mcu": mcu,
        "params": params,
    }

    env = (f"LAGER_COMMAND_DATA={json.dumps(data)}",) + extra_env

    run_python_internal(
        ctx,
        get_impl_path(impl_script),
        box,
        env=env,
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=timeout,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


def run_impl_script(
    ctx: click.Context,
    box: str,
    impl_script: str,
    args: tuple[str, ...] = (),
    env: tuple[str, ...] = (),
    timeout: int | None = 0,
) -> None:
    """
    Run an implementation script with explicit arguments (no LAGER_COMMAND_DATA).

    This function runs an impl script passing arguments directly rather
    than through the LAGER_COMMAND_DATA environment variable. Useful for
    scripts that use traditional command-line argument parsing.

    Args:
        ctx: Click context object
        box: Box IP address
        impl_script: Name of the implementation script
        args: Arguments to pass to the script
        env: Environment variables as tuple of "KEY=value" strings
        timeout: Timeout in seconds (None for no timeout, 0 for default)

    Example:
        run_impl_script(
            ctx, box_ip, "eload.py",
            args=("cc", "eload1", "0.5"),
        )
    """
    from ..context import get_impl_path
    from ..commands.development.python import run_python_internal

    run_python_internal(
        ctx,
        get_impl_path(impl_script),
        box,
        env=env,
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=timeout,
        detach=False,
        port=(),
        org=None,
        args=args,
    )


# =============================================================================
# Validation Helpers
# =============================================================================

def validate_positive_float(
    value: float | None,
    param_name: str,
    unit: str = "",
) -> None:
    """
    Validate that a float value is positive (if provided).

    Args:
        value: The value to validate (None is allowed)
        param_name: Name of the parameter for error message
        unit: Unit suffix for error message (e.g., "V", "A")

    Raises:
        click.BadParameter: If value is negative

    Example:
        validate_positive_float(voltage, "voltage", "V")
        validate_positive_float(current, "current", "A")
    """
    if value is not None and value < 0:
        unit_str = f"{unit}" if unit else ""
        raise click.BadParameter(
            f"{param_name.replace('_', ' ').title()} must be positive, got {value}{unit_str}"
        )


def validate_positive_parameters(**params: float | None) -> None:
    """
    Validate that all provided parameters are positive values.

    Args:
        **params: Named parameters to validate. Names containing "voltage"
                  or "ovp" use "V" as unit, others use "A".

    Raises:
        click.BadParameter: If any value is negative

    Example:
        validate_positive_parameters(voltage=3.3, ocp=0.5, ovp=5.5)
    """
    for param_name, value in params.items():
        if value is not None and value < 0:
            if "voltage" in param_name or "ovp" in param_name:
                unit = "V"
            else:
                unit = "A"
            raise click.BadParameter(
                f"{param_name.replace('_', ' ').title()} must be positive, got {value}{unit}"
            )


def validate_protection_limits(
    voltage: float | None = None,
    current: float | None = None,
    ovp: float | None = None,
    ocp: float | None = None,
) -> None:
    """
    Validate that protection limits are not below setpoints.

    This function ensures that over-voltage protection (OVP) is not set
    lower than the voltage setpoint, and over-current protection (OCP)
    is not set lower than the current setpoint.

    Args:
        voltage: Voltage setpoint (V)
        current: Current setpoint (A)
        ovp: Over-voltage protection limit (V)
        ocp: Over-current protection limit (A)

    Raises:
        click.BadParameter: If protection limit is below setpoint

    Example:
        validate_protection_limits(voltage=3.3, ovp=5.0)  # OK
        validate_protection_limits(voltage=5.0, ovp=3.3)  # Raises error
    """
    if ovp is not None and voltage is not None and ovp < voltage:
        raise click.BadParameter(
            f"OVP limit ({ovp}V) cannot be less than voltage setpoint ({voltage}V). "
            f"Use a higher OVP value or lower the voltage first."
        )
    if ocp is not None and current is not None and ocp < current:
        raise click.BadParameter(
            f"OCP limit ({ocp}A) cannot be less than current setpoint ({current}A). "
            f"Use a higher OCP value or lower the current first."
        )


# =============================================================================
# Callback Helpers for Click Options
# =============================================================================

def parse_value_with_negatives(
    ctx: click.Context,
    param: click.Parameter,
    value: str | float | None,
) -> float | None:
    """
    Parse value, handling the case where negative values are passed with -- separator.

    Click callback function that properly handles negative float values
    that may be passed using the '--' separator to distinguish them from flags.

    Args:
        ctx: Click context object
        param: Click parameter object
        value: Value to parse (may start with '--' for negative numbers)

    Returns:
        Parsed float value or None

    Raises:
        click.BadParameter: If value cannot be parsed as float

    Example:
        @click.option("--value", callback=parse_value_with_negatives)
        def my_command(value):
            # value is now a properly parsed float
            pass
    """
    if value is None:
        return None

    # Handle case where value looks like '--0.050' (negative value with -- separator)
    if isinstance(value, str) and value.startswith('--') and len(value) > 2:
        try:
            # Remove the extra '--' prefix and parse as negative
            return -float(value[2:])
        except ValueError:
            pass

    # Regular parsing
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            raise click.BadParameter(f"'{value}' is not a valid float.")

    return float(value) if value is not None else None


# =============================================================================
# Role Constants (for reference and consistency)
# =============================================================================

# Common net roles used across the CLI
NET_ROLES = {
    "power_supply": "power-supply",
    "battery": "battery",
    "solar": "solar",
    "eload": "eload",
    "scope": "scope",
    "logic": "logic",
    "thermocouple": "thermocouple",
    "watt_meter": "watt-meter",
    "adc": "adc",
    "dac": "dac",
    "gpi": "gpi",
    "gpo": "gpo",
}


def get_role(role_key: str) -> str:
    """
    Get the actual role string for a role key.

    Args:
        role_key: Role key (e.g., "power_supply", "battery")

    Returns:
        Role string (e.g., "power-supply", "battery")

    Example:
        role = get_role("power_supply")  # Returns "power-supply"
    """
    return NET_ROLES.get(role_key, role_key)


# =============================================================================
# Net Existence Validation
# =============================================================================

def validate_net_exists(
    ctx: click.Context,
    box: str,
    netname: str,
    role: str,
    exit_on_error: bool = True,
) -> dict | None:
    """
    Validate that a net exists with the specified role.

    Routes errors through cli.output.error so the message respects the
    --format=text|json policy. When the net exists but has the wrong role
    (e.g. ``lager battery supply2 state`` where ``supply2`` is a
    power-supply net) the envelope includes ``actual_role`` so JSON
    consumers can distinguish "missing" from "wrong-role".

    Args:
        ctx: Click context object
        box: Box IP address
        netname: Name of the net to validate
        role: Expected role (e.g., 'adc', 'dac', 'power-supply')
        exit_on_error: If True, calls output.error (which raises SystemExit)

    Returns:
        Net dict if found, None if not found and exit_on_error=False
    """
    from ..output import error as output_error, ExitCode
    cfg = ctx.obj.output if ctx.obj is not None and hasattr(ctx.obj, "output") else None

    nets = list_nets_by_role(ctx, box, role)
    matching = next((n for n in nets if n.get('name') == netname), None)
    if matching:
        return matching

    # Distinguish missing vs wrong-role: scan all nets to find an actual_role.
    actual_role = None
    try:
        all_nets = run_net_py(ctx, box, "list")
        actual = next((n for n in all_nets if n.get('name') == netname), None)
        if actual is not None:
            actual_role = actual.get('role')
    except Exception:
        pass

    available = [n.get('name') for n in nets]
    if actual_role is not None and actual_role != role:
        message = f"'{netname}' is a {actual_role} net, expected {role}"
        data = {
            "actual_role": actual_role,
            "expected_role": role,
            "available": available,
        }
    elif available:
        message = (
            f"Net '{netname}' with role '{role}' not found. "
            f"Available {role} nets: {', '.join(available)}"
        )
        data = {"expected_role": role, "available": available}
    else:
        message = (
            f"Net '{netname}' with role '{role}' not found. "
            f"No {role} nets are configured."
        )
        data = {"expected_role": role, "available": []}

    # Build a dotted command path by walking the click context chain so
    # JSON consumers see "battery.state" rather than just "state".
    def _dotted_command(c):
        parts = []
        cur = c
        while cur is not None and getattr(cur, "info_name", None):
            # Skip the root group ("cli") — it's not user-meaningful.
            if cur.parent is None:
                break
            parts.append(cur.info_name)
            cur = cur.parent
        return ".".join(reversed(parts)) if parts else None

    cmd_path = _dotted_command(ctx)

    if exit_on_error:
        output_error(
            message, cfg=cfg,
            exit_code=ExitCode.USER_ERROR,
            command=cmd_path,
            subject={"net": netname, "box": box},
            data=data,
        )
    else:
        output_error(
            message, cfg=cfg,
            exit_code=ExitCode.USER_ERROR,
            command=cmd_path,
            subject={"net": netname, "box": box},
            data=data,
            raise_exit=False,
        )
    return None


def validate_range(
    ctx: click.Context,
    value: float,
    min_val: float,
    max_val: float,
    param_name: str,
    unit: str = "",
) -> None:
    """
    Validate that a value is within the specified range.

    Args:
        ctx: Click context object
        value: The value to validate
        min_val: Minimum allowed value
        max_val: Maximum allowed value
        param_name: Name of the parameter for error message
        unit: Unit suffix for error message (e.g., "V", "A", "%")

    Example:
        validate_range(ctx, soc, 0, 100, "SOC", "%")
        validate_range(ctx, voltage, 0, 10, "voltage", "V")
    """
    if value < min_val or value > max_val:
        unit_str = f" {unit}" if unit else ""
        click.secho(
            f"Error: {param_name} must be between {min_val} and {max_val}{unit_str}, got {value}",
            fg='red', err=True
        )
        ctx.exit(1)
