# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Electronic load CLI commands.

Usage:
    lager eload                     -> lists electronic load nets
    lager eload <NETNAME> cc 0.5    -> set constant current to 0.5A
    lager eload <NETNAME> cv 12.0   -> set constant voltage to 12V
    lager eload <NETNAME> cr 100    -> set constant resistance to 100 ohms
    lager eload <NETNAME> cp 10     -> set constant power to 10W
    lager eload <NETNAME> state     -> display electronic load state
"""
from __future__ import annotations

import click

# Import consolidated helpers from cli.core.net_helpers
from ...core.net_helpers import (
    require_netname,
    resolve_box,
    display_nets,
    run_impl_script,
    NET_ROLES,
)
from ...context import get_default_net
from ...options import force_command_option


ELOAD_ROLE = NET_ROLES["eload"]  # "eload"

# Electronic load range limits (based on common equipment like Rigol DL3021)
# These are reasonable defaults that can be adjusted for specific equipment
ELOAD_LIMITS = {
    "cc": {"min": 0, "max": 40, "unit": "A", "name": "current"},      # Constant Current
    "cv": {"min": 0, "max": 150, "unit": "V", "name": "voltage"},     # Constant Voltage
    "cr": {"min": 0.03, "max": 10000, "unit": "ohms", "name": "resistance"},  # Constant Resistance (min ~30mOhm)
    "cp": {"min": 0, "max": 200, "unit": "W", "name": "power"},       # Constant Power
}


def _validate_eload_value(ctx, mode, value):
    """Validate electronic load value is within acceptable range."""
    if value is None:
        return  # Read operation, no validation needed

    limits = ELOAD_LIMITS.get(mode)
    if not limits:
        return

    if value < limits["min"]:
        click.secho(
            f"Error: {limits['name'].title()} must be >= {limits['min']} {limits['unit']}, got {value}",
            fg='red', err=True
        )
        if mode == "cr" and value == 0:
            click.echo("Note: Resistance cannot be 0 (would cause infinite current)", err=True)
        ctx.exit(1)

    if value > limits["max"]:
        click.secho(
            f"Error: {limits['name'].title()} must be <= {limits['max']} {limits['unit']}, got {value}",
            fg='red', err=True
        )
        click.echo(f"This limit protects equipment from damage. Check your equipment specs.", err=True)
        ctx.exit(1)


# ---------- CLI ----------

@click.group(invoke_without_command=True)
@click.argument('netname', required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
@force_command_option
def eload(ctx, netname, box):
    """Control electronic load settings and modes"""
    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'eload')

    if netname is not None:
        ctx.obj.netname = netname

    # If no subcommand and no netname, list nets
    if ctx.invoked_subcommand is None:
        resolved_box = resolve_box(ctx, box)
        display_nets(ctx, resolved_box, None, ELOAD_ROLE, "electronic load")


@eload.command()
@click.argument('value', required=False, type=float)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def cc(ctx, value, box):
    """Set (or read) constant current mode in amps (A)"""
    _validate_eload_value(ctx, "cc", value)
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "eload")
    args = ["cc", netname]
    if value is not None:
        args.append(str(value))
    run_impl_script(ctx, resolved_box, 'eload.py', args=tuple(args))


@eload.command()
@click.argument('value', required=False, type=float)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def cv(ctx, value, box):
    """Set (or read) constant voltage mode in volts (V)"""
    _validate_eload_value(ctx, "cv", value)
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "eload")
    args = ["cv", netname]
    if value is not None:
        args.append(str(value))
    run_impl_script(ctx, resolved_box, 'eload.py', args=tuple(args))


@eload.command()
@click.argument('value', required=False, type=float)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def cr(ctx, value, box):
    """Set (or read) constant resistance mode in ohms"""
    _validate_eload_value(ctx, "cr", value)
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "eload")
    args = ["cr", netname]
    if value is not None:
        args.append(str(value))
    run_impl_script(ctx, resolved_box, 'eload.py', args=tuple(args))


@eload.command()
@click.argument('value', required=False, type=float)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def cp(ctx, value, box):
    """Set (or read) constant power mode in watts (W)"""
    _validate_eload_value(ctx, "cp", value)
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "eload")
    args = ["cp", netname]
    if value is not None:
        args.append(str(value))
    run_impl_script(ctx, resolved_box, 'eload.py', args=tuple(args))


@eload.command()
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def state(ctx, box):
    """Display electronic load state"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "eload")
    args = ["state", netname]
    run_impl_script(ctx, resolved_box, 'eload.py', args=tuple(args))
