# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Battery simulator CLI commands.

Usage:
    lager battery                     -> lists battery nets
    lager battery <NETNAME> soc 80    -> set state of charge to 80%
    lager battery <NETNAME> voc 4.2   -> set open circuit voltage
    lager battery <NETNAME> enable
    lager battery <NETNAME> disable
    lager battery <NETNAME> state
"""
from __future__ import annotations

import json
import asyncio

import click

from ...core.net_helpers import (
    require_netname,
    resolve_box,
    validate_net,
    validate_net_exists,
    display_nets,
    NET_ROLES,
)
from ...context import get_impl_path, get_default_net
from ...output import ExitCode, action as output_action, error as output_error
from ..development.python import run_python_internal


BATTERY_ROLE = NET_ROLES["battery"]  # "battery"


def _backend_env(ctx) -> tuple[str, ...]:
    """Forward output presentation env vars to the impl script."""
    cfg = ctx.obj.output
    return (
        f"LAGER_OUTPUT_FORMAT={cfg.format.value}",
        f"LAGER_OUTPUT_COLOR={'1' if cfg.color else '0'}",
    )


def _validate_or_exit(ctx, box, netname):
    """Verify the net exists with role=battery, routing the error through output.error."""
    return validate_net_exists(ctx, box, netname, BATTERY_ROLE)


def _parse_float_or_exit(ctx, value, *, command, label, must_be_positive=False,
                          must_be_non_negative=False, max_value=None, range_0_100=False,
                          unit="") -> float | None:
    if value is None:
        return None
    cfg = ctx.obj.output
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        output_error(f"'{value}' is not a valid number for {label}",
                     cfg=cfg, exit_code=ExitCode.USAGE,
                     command=command, data={"value": value})
        return None
    if range_0_100 and (parsed < 0 or parsed > 100):
        output_error(f"{label} must be between 0 and 100%, got {parsed}%",
                     cfg=cfg, exit_code=ExitCode.USER_ERROR,
                     command=command, data={"value": parsed})
    if must_be_positive and parsed <= 0:
        output_error(f"{label} must be positive, got {parsed} {unit}".strip(),
                     cfg=cfg, exit_code=ExitCode.USER_ERROR,
                     command=command, data={"value": parsed})
    if must_be_non_negative and parsed < 0:
        output_error(f"{label} must be non-negative, got {parsed} {unit}".strip(),
                     cfg=cfg, exit_code=ExitCode.USER_ERROR,
                     command=command, data={"value": parsed})
    if max_value is not None and parsed > max_value:
        output_error(f"{label} must not exceed {max_value} {unit}, got {parsed} {unit}".strip(),
                     cfg=cfg, exit_code=ExitCode.USER_ERROR,
                     command=command, data={"value": parsed, "limit": max_value})
    return parsed


# ---------- Battery-specific backend runner ----------

def _run_backend(ctx, box, action: str, **params):
    """
    Run backend command, preferring the WebSocket fast-path when a TUI is
    holding the supply, falling back to running the impl script directly.
    """
    import requests

    cfg = ctx.obj.output
    netname = params.get('netname')
    subject = {"net": netname} if netname else None
    cmd_path = f"battery.{action}"

    # See note in supply.py — skip WS fast-path for state so we render
    # via the structured cli_output path on the box.
    skip_ws = action in ("print_state", "state")

    # Try WebSocket HTTP endpoint first (for concurrent TUI + CLI access)
    if netname and not skip_ws:
        try:
            from ...box_storage import resolve_and_validate_box
            box_ip = resolve_and_validate_box(ctx, box)
            url = f"http://{box_ip}:9000/battery/command"
            payload = {"netname": netname, "action": action, "params": params}
            response = requests.post(url, json=payload, timeout=10)

            if response.status_code == 200:
                result = response.json()
                if result.get('success'):
                    output_action(result.get('message', 'Command executed'),
                                  cfg=cfg, command=cmd_path, subject=subject)
                    return
                output_error(
                    result.get('error', 'Unknown error'),
                    cfg=cfg, exit_code=ExitCode.BACKEND_ERROR,
                    command=cmd_path, subject=subject,
                )
            elif response.status_code == 404:
                pass  # No active WS session; fall through.
        except (requests.ConnectionError, requests.Timeout):
            pass
        except Exception:
            pass

    # Fall back to direct USB access via the impl script.
    data = {'action': action, 'params': params}
    run_python_internal(
        ctx,
        get_impl_path('battery.py'),
        box,
        env=(
            f'LAGER_COMMAND_DATA={json.dumps(data)}',
            *_backend_env(ctx),
        ),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum='SIGTERM',
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )

    # For action commands, the impl is silent — emit an ack so users (and JSON
    # consumers) get a structured result. The state command's impl emits its
    # own envelope so we skip the ack here.
    if action in (
        "set_mode", "set_to_battery_mode",
        "set_soc", "set_voc", "set_volt_full", "set_volt_empty",
        "set_capacity", "set_current_limit", "set_ovp", "set_ocp",
        "set_model",
        "enable_battery", "disable_battery",
        "clear", "clear_ovp", "clear_ocp",
    ):
        ack_messages = {
            "set_mode": "Battery mode updated",
            "set_to_battery_mode": "Initialized battery simulator mode",
            "set_soc": "State of charge updated",
            "set_voc": "Open-circuit voltage updated",
            "set_volt_full": "Battery-full voltage updated",
            "set_volt_empty": "Battery-empty voltage updated",
            "set_capacity": "Capacity limit updated",
            "set_current_limit": "Current limit updated",
            "set_ovp": "OVP limit updated",
            "set_ocp": "OCP limit updated",
            "set_model": "Battery model updated",
            "enable_battery": "Enabled battery output",
            "disable_battery": "Disabled battery output",
            "clear": "Cleared protection trips",
            "clear_ovp": "Cleared OVP trip",
            "clear_ocp": "Cleared OCP trip",
        }
        output_action(ack_messages.get(action, "Operation completed"),
                      cfg=cfg, command=cmd_path, subject=subject)


# ---------- CLI ----------

@click.group(invoke_without_command=True)
@click.argument('NETNAME', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def battery(ctx, box, netname):
    """Control battery simulator settings and output"""
    if netname is None:
        netname = get_default_net(ctx, 'battery')

    if netname is not None:
        ctx.obj.netname = netname

    if ctx.invoked_subcommand is None:
        resolved_box = resolve_box(ctx, box)
        display_nets(ctx, resolved_box, None, BATTERY_ROLE, "battery")


@battery.command()
@click.argument('MODE_TYPE', required=False, type=click.Choice(('static', 'dynamic')))
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def mode(ctx, box, mode_type):
    """Set (or read) battery simulation mode type"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    _run_backend(ctx, resolved_box, 'set_mode', netname=netname, mode_type=mode_type)


@battery.command(name='set')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def set_mode(ctx, box):
    """Initialize battery simulator mode"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    _run_backend(ctx, resolved_box, 'set_to_battery_mode', netname=netname)


@battery.command()
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def soc(ctx, box, value):
    """Set (or read) battery state of charge in percent (%)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    parsed = _parse_float_or_exit(ctx, value, command="battery.soc", label="SOC",
                                   range_0_100=True, unit="%")
    _run_backend(ctx, resolved_box, 'set_soc', netname=netname, value=parsed)


@battery.command()
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def voc(ctx, box, value):
    """Set (or read) battery open circuit voltage in volts (V)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    parsed = _parse_float_or_exit(ctx, value, command="battery.voc", label="VOC",
                                   must_be_non_negative=True, unit="V")
    _run_backend(ctx, resolved_box, 'set_voc', netname=netname, value=parsed)


@battery.command(name='batt-full')
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def batt_full(ctx, box, value):
    """Set (or read) battery fully charged voltage in volts (V)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    parsed = _parse_float_or_exit(ctx, value, command="battery.batt_full",
                                   label="Battery-full voltage",
                                   must_be_non_negative=True, unit="V")
    _run_backend(ctx, resolved_box, 'set_volt_full', netname=netname, value=parsed)


@battery.command(name='batt-empty')
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def batt_empty(ctx, box, value):
    """Set (or read) battery fully discharged voltage in volts (V)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    parsed = _parse_float_or_exit(ctx, value, command="battery.batt_empty",
                                   label="Battery-empty voltage",
                                   must_be_non_negative=True, unit="V")
    _run_backend(ctx, resolved_box, 'set_volt_empty', netname=netname, value=parsed)


@battery.command()
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def capacity(ctx, box, value):
    """Set (or read) battery capacity limit in amp-hours (Ah)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    parsed = _parse_float_or_exit(ctx, value, command="battery.capacity",
                                   label="Capacity",
                                   must_be_positive=True, unit="Ah")
    _run_backend(ctx, resolved_box, 'set_capacity', netname=netname, value=parsed)


@battery.command(name='current-limit')
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def current_limit(ctx, box, value):
    """Set (or read) maximum charge/discharge current in amps (A)"""
    # Keithley 2281S max current is 6A
    MAX_CURRENT = 6.0
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    parsed = _parse_float_or_exit(ctx, value, command="battery.current_limit",
                                   label="Current limit",
                                   must_be_non_negative=True, max_value=MAX_CURRENT,
                                   unit="A")
    _run_backend(ctx, resolved_box, 'set_current_limit', netname=netname, value=parsed)


@battery.command()
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def ovp(ctx, box, value):
    """Set (or read) over voltage protection limit in volts (V)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    parsed = _parse_float_or_exit(ctx, value, command="battery.ovp", label="OVP",
                                   must_be_positive=True, unit="V")
    _run_backend(ctx, resolved_box, 'set_ovp', netname=netname, value=parsed)


@battery.command()
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def ocp(ctx, box, value):
    """Set (or read) over current protection limit in amps (A)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    parsed = _parse_float_or_exit(ctx, value, command="battery.ocp", label="OCP",
                                   must_be_positive=True, unit="A")
    _run_backend(ctx, resolved_box, 'set_ocp', netname=netname, value=parsed)


@battery.command()
@click.argument('PARTNUMBER', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def model(ctx, box, partnumber):
    """Set (or read) battery model (18650, nimh, lead-acid, etc.)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    _run_backend(ctx, resolved_box, 'set_model', netname=netname, partnumber=partnumber)


@battery.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def state(ctx, box):
    """Get battery state (comprehensive status)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    _run_backend(ctx, resolved_box, 'print_state', netname=netname)


@battery.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--yes', is_flag=True, help='Confirm the action without prompting.')
def enable(ctx, box, yes):
    """Enable battery simulator output"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)

    if not (yes or click.confirm("Enable Net?", default=False)):
        click.echo("Aborting")
        return

    _run_backend(ctx, resolved_box, 'enable_battery', netname=netname)


@battery.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--yes', is_flag=True, help='Confirm the action without prompting.')
def disable(ctx, box, yes):
    """Disable battery simulator output"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)

    if not (yes or click.confirm("Disable Net?", default=True)):
        click.echo("Aborting")
        return

    _run_backend(ctx, resolved_box, 'disable_battery', netname=netname)


# --------- CLEAR COMMANDS ---------

@battery.command(name='clear')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def clear_both(ctx, box):
    """Clear protection trip conditions (OVP/OCP)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    _run_backend(ctx, resolved_box, 'clear', netname=netname)


@battery.command(name='clear-ovp')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def clear_ovp(ctx, box):
    """Clear OVP trip condition"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    _run_backend(ctx, resolved_box, 'clear_ovp', netname=netname)


@battery.command(name='clear-ocp')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def clear_ocp(ctx, box):
    """Clear OCP trip condition"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)
    _run_backend(ctx, resolved_box, 'clear_ocp', netname=netname)


@battery.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def tui(ctx, box):
    """Launch interactive battery control TUI"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _validate_or_exit(ctx, resolved_box, netname)

    try:
        from ...battery.battery_tui import BatteryTUI
        app = BatteryTUI(ctx, netname, resolved_box, resolved_box)
        asyncio.run(app.run_async())
    except Exception:
        raise
