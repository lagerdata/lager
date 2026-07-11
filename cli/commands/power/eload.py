# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Electronic load CLI commands.

Usage:
    lager eload                     -> lists electronic load nets
    lager eload [NET_NAME] cc 0.5    -> set constant current to 0.5A
    lager eload [NET_NAME] cv 12.0   -> set constant voltage to 12V
    lager eload [NET_NAME] cr 100    -> set constant resistance to 100 ohms
    lager eload [NET_NAME] cp 10     -> set constant power to 10W
    lager eload [NET_NAME] state     -> display electronic load state
"""
from __future__ import annotations

import json

import click

from ...core.net_group import NetGroup
# Import consolidated helpers from cli.core.net_helpers
from ...core.net_helpers import (
    require_netname,
    resolve_box,
    display_nets,
    post_net_command,
    NET_ROLES,
)
from ...context import get_default_net


ELOAD_ROLE = NET_ROLES["eload"]  # "eload"

# Electronic load range limits (based on common equipment like Rigol DL3021)
# These are reasonable defaults that can be adjusted for specific equipment
ELOAD_LIMITS = {
    "cc": {"min": 0, "max": 40, "unit": "A", "name": "current"},      # Constant Current
    "cv": {"min": 0, "max": 150, "unit": "V", "name": "voltage"},     # Constant Voltage
    "cr": {"min": 0.03, "max": 10000, "unit": "ohms", "name": "resistance"},  # Constant Resistance (min ~30mOhm)
    "cp": {"min": 0, "max": 200, "unit": "W", "name": "power"},       # Constant Power
}

_MODE_META = {
    "cc": ("CC", "current", "A"),
    "cv": ("CV", "voltage", "V"),
    "cr": ("CR", "resistance", "Ω"),
    "cp": ("CP", "power", "W"),
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


def _format_mode_result(mode: str, value: float, *, is_set: bool) -> str:
    """Human-readable one-liner for cc/cv/cr/cp set/read (matches old impl)."""
    mode_name, field, unit = _MODE_META[mode]
    label = field.title()
    parts = [f"Mode: {mode_name}"]
    if is_set or mode_name:
        parts.append(f"{label}: {value} {unit}")
    return ", ".join(parts)


def _print_mode_result(mode: str, value: float, *, is_set: bool) -> None:
    click.secho(_format_mode_result(mode, value, is_set=is_set), fg="green")


def _print_eload_state(state: dict) -> None:
    """Multi-line state block matching rigol_dl3021.print_state()."""
    input_state = "Enabled" if state.get("input_enabled") else "Disabled"
    mode = state.get("mode", "?")

    click.secho("Electronic Load State:", fg="green")
    click.secho(f"  Mode: {mode}", fg="green")
    click.secho(f"  Input: {input_state}", fg="green")
    click.secho(
        f"  Measured Voltage: {state['measured_voltage']:.3f} V", fg="green")
    click.secho(
        f"  Measured Current: {state['measured_current']:.3f} A", fg="green")
    click.secho(
        f"  Measured Power: {state['measured_power']:.3f} W", fg="green")

    if mode == "CC" and "current_setting" in state:
        click.secho(
            f"  Current Setting: {state['current_setting']:.3f} A", fg="green")
    elif mode == "CV" and "voltage_setting" in state:
        click.secho(
            f"  Voltage Setting: {state['voltage_setting']:.3f} V", fg="green")
    elif mode == "CR" and "resistance_setting" in state:
        click.secho(
            f"  Resistance Setting: {state['resistance_setting']:.3f} Ω",
            fg="green")
    elif mode in ("CW", "CP") and "power_setting" in state:
        click.secho(
            f"  Power Setting: {state['power_setting']:.3f} W", fg="green")


def _run_eload(ctx, box_ip, netname, mode, value, as_json=False):
    """Drive an e-load mode via the box's :9000 /net/command endpoint.

    ``value`` present -> set that mode's setpoint; absent -> read it back.
    """
    params = {}
    if value is not None:
        params["value"] = value
    result = post_net_command(ctx, box_ip, netname, mode, role="eload",
                              quiet=True, **params)
    setpoint = float(result.get("value"))

    if as_json:
        mode_name, field, _ = _MODE_META[mode]
        click.echo(json.dumps({
            "netname": netname,
            "mode": mode_name,
            field: setpoint,
            "action": "set" if value is not None else "read",
        }))
    else:
        _print_mode_result(mode, setpoint, is_set=value is not None)


# ---------- CLI ----------

@click.group(cls=NetGroup, invoke_without_command=True)
@click.argument('netname', required=False, metavar="[NET_NAME]")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
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


eload.net_examples = [
    "lager eload eload1 cc 0.5 --box <BOX>",
    "lager eload eload1 cv 3.3 --box <BOX>",
    "lager eload eload1 state --box <BOX>",
    "lager eload eload1 cc 0.5 --json --box <BOX>",
    "lager eload --box <BOX>                (list electronic load nets)",
]


def _mode_options(func):
    func = click.option("--json", "as_json", is_flag=True, default=False,
                        help="Emit a machine-readable JSON object instead of formatted text")(func)
    func = click.option("--box", required=False, help="Lagerbox name or IP")(func)
    return func


@eload.command()
@click.argument('value', required=False, type=float)
@_mode_options
@click.pass_context
def cc(ctx, value, box, as_json):
    """Set (or read) constant current mode in amps (A)"""
    _validate_eload_value(ctx, "cc", value)
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "eload")
    _run_eload(ctx, resolved_box, netname, "cc", value, as_json)


@eload.command()
@click.argument('value', required=False, type=float)
@_mode_options
@click.pass_context
def cv(ctx, value, box, as_json):
    """Set (or read) constant voltage mode in volts (V)"""
    _validate_eload_value(ctx, "cv", value)
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "eload")
    _run_eload(ctx, resolved_box, netname, "cv", value, as_json)


@eload.command()
@click.argument('value', required=False, type=float)
@_mode_options
@click.pass_context
def cr(ctx, value, box, as_json):
    """Set (or read) constant resistance mode in ohms"""
    _validate_eload_value(ctx, "cr", value)
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "eload")
    _run_eload(ctx, resolved_box, netname, "cr", value, as_json)


@eload.command()
@click.argument('value', required=False, type=float)
@_mode_options
@click.pass_context
def cp(ctx, value, box, as_json):
    """Set (or read) constant power mode in watts (W)"""
    _validate_eload_value(ctx, "cp", value)
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "eload")
    _run_eload(ctx, resolved_box, netname, "cp", value, as_json)


@eload.command()
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON object instead of formatted text")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def state(ctx, box, as_json):
    """Display electronic load state"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "eload")
    result = post_net_command(ctx, resolved_box, netname, "state",
                              role="eload", quiet=True)
    load_state = result.get("value") or {}

    if as_json:
        click.echo(json.dumps({"netname": netname, **load_state}))
    else:
        _print_eload_state(load_state)
