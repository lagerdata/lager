# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Battery simulator CLI commands.

Usage:
    lager battery                     -> lists battery nets
    lager battery [NET_NAME] soc 80    -> set state of charge to 80%
    lager battery [NET_NAME] voc 4.2   -> set open circuit voltage
    lager battery [NET_NAME] enable
    lager battery [NET_NAME] disable
    lager battery [NET_NAME] state
"""
from __future__ import annotations

import json
import asyncio

import click

from ...core.net_group import NetGroup
# Import consolidated helpers from cli.core.net_helpers
from ...core.net_helpers import (
    require_netname,
    resolve_box,
    validate_net,
    display_nets,
    run_backend,
    NET_ROLES,
)
from ...context import get_impl_path, get_default_net
from ...errors import LagerError, connection_error, is_connection_error
from ..development.python import run_python_internal
from .battery_model_csv import parse_model_csv, write_model_csv


BATTERY_ROLE = NET_ROLES["battery"]  # "battery"


# ---------- Battery-specific backend runner ----------

def _run_backend(ctx, box, action: str, **params):
    """
    Run backend command and handle errors gracefully.

    First tries to use the WebSocket HTTP endpoint if a TUI is running for this net,
    which allows sharing the USB connection. Falls back to direct access if no TUI is active.
    """
    import requests

    netname = params.get('netname')

    # Try WebSocket HTTP endpoint first (for concurrent TUI + CLI access)
    if netname:
        try:
            # Get box IP
            from ...box_storage import resolve_and_validate_box
            box_ip = resolve_and_validate_box(ctx, box)

            # Try the WebSocket-shared endpoint
            url = f"http://{box_ip}:9000/battery/command"
            payload = {
                "netname": netname,
                "action": action,
                "params": params
            }

            response = requests.post(url, json=payload, timeout=10)

            if response.status_code == 200:
                result = response.json()
                if result.get('success'):
                    # Command succeeded via WebSocket endpoint
                    message = result.get('message', 'Command executed')
                    click.echo(f"\033[92m{message}\033[0m")
                    return
                else:
                    # WebSocket endpoint returned error
                    click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
                    raise SystemExit(1)

            elif response.status_code == 404:
                # No active WebSocket session, fall through to direct access
                pass

            else:
                # Other HTTP error, try direct access as fallback
                pass

        except (requests.ConnectionError, requests.Timeout):
            # Box not reachable via HTTP, fall through to direct access
            pass
        except Exception:
            # Other error, fall through to direct access
            pass

    # Fall back to direct USB access (original behavior)
    data = {
        'action': action,
        'params': params,
    }
    run_python_internal(
        ctx,
        get_impl_path('battery.py'),
        box,
        env=(f'LAGER_COMMAND_DATA={json.dumps(data)}',),
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


def _battery_command_request(ctx, box, action: str, timeout: int = 30, **params):
    """POST an action to the box's /battery/command endpoint and return the
    parsed JSON result.

    Unlike _run_backend — which only echoes result['message'] and falls back
    to the impl-script path — this gives the caller the structured payload
    (e.g. result['models'], result['model']), which model-create's occupancy
    guard and model-export's CSV writer need. There is no fallback: these
    actions require the box HTTP endpoint.
    """
    import requests
    from ...box_storage import resolve_and_validate_box

    box_ip = resolve_and_validate_box(ctx, box)
    url = f"http://{box_ip}:9000/battery/command"
    payload = {"netname": params.get('netname'), "action": action, "params": params}
    try:
        response = requests.post(url, json=payload, timeout=timeout)
    except Exception as exc:
        if is_connection_error(exc):
            raise connection_error(exc, host=box_ip)
        raise
    if response.status_code != 200:
        body = ""
        try:
            body = response.json().get('error', '')
        except Exception:
            pass
        if response.status_code == 400 and 'Unknown action' in body:
            raise LagerError(
                f"The box does not support the battery '{action}' action yet.",
                cause="Its Lager software predates this command.",
                fixes=[f"lager update --box {box}"],
            )
        raise LagerError(
            f"Box battery endpoint returned HTTP {response.status_code}.",
            cause=body or None,
        )
    result = response.json()
    if not result.get('success'):
        raise LagerError(result.get('error', 'Unknown error'))
    return result


# ---------- CLI ----------

@click.group(cls=NetGroup, invoke_without_command=True)
@click.argument('NETNAME', required=False, metavar="[NET_NAME]")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def battery(ctx, box, netname):
    """Control battery simulator settings and output"""
    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'battery')

    if netname is not None:
        ctx.obj.netname = netname

    # Only resolve box if no subcommand (listing nets)
    if ctx.invoked_subcommand is None:
        resolved_box = resolve_box(ctx, box)
        display_nets(ctx, resolved_box, None, BATTERY_ROLE, "battery")


battery.net_examples = [
    "lager battery batt1 soc 80 --box <BOX>",
    "lager battery batt1 voc 3.7 --box <BOX>",
    "lager battery batt1 enable --box <BOX>",
    "lager battery batt1 state --box <BOX>",
    "lager battery --box <BOX>              (list battery nets)",
]


@battery.command()
@click.argument('MODE_TYPE', required=False, type=click.Choice(('static', 'dynamic')))
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def mode(ctx, box, mode_type):
    """Set (or read) battery simulation mode type"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'set_mode', netname=netname, mode_type=mode_type)


@battery.command(name='set')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def set_mode(ctx, box):
    """Initialize battery simulator mode"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'set_to_battery_mode', netname=netname)


@battery.command()
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def soc(ctx, box, value):
    """Set (or read) battery state of charge in percent (%)"""
    # Parse and validate SOC
    parsed_value = None
    if value is not None:
        try:
            parsed_value = float(value)
        except ValueError:
            click.secho(f"Error: '{value}' is not a valid number", fg='red', err=True)
            ctx.exit(1)
        if parsed_value < 0 or parsed_value > 100:
            click.secho(f"Error: SOC must be between 0 and 100%, got {parsed_value}%", fg='red', err=True)
            ctx.exit(1)

    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'set_soc', netname=netname, value=parsed_value)


@battery.command()
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def voc(ctx, box, value):
    """Set (or read) battery open circuit voltage in volts (V)"""
    # Parse and validate VOC
    parsed_value = None
    if value is not None:
        try:
            parsed_value = float(value)
        except ValueError:
            click.secho(f"Error: '{value}' is not a valid number", fg='red', err=True)
            ctx.exit(1)
        if parsed_value < 0:
            click.secho(f"Error: VOC must be positive, got {parsed_value} V", fg='red', err=True)
            ctx.exit(1)

    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'set_voc', netname=netname, value=parsed_value)


@battery.command(name='batt-full')
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def batt_full(ctx, box, value):
    """Set (or read) battery fully charged voltage in volts (V)"""
    # Parse and validate voltage
    parsed_value = None
    if value is not None:
        try:
            parsed_value = float(value)
        except ValueError:
            click.secho(f"Error: '{value}' is not a valid number", fg='red', err=True)
            ctx.exit(1)
        if parsed_value < 0:
            click.secho(f"Error: Battery full voltage must be positive, got {parsed_value} V", fg='red', err=True)
            ctx.exit(1)

    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'set_volt_full', netname=netname, value=parsed_value)


@battery.command(name='batt-empty')
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def batt_empty(ctx, box, value):
    """Set (or read) battery fully discharged voltage in volts (V)"""
    # Parse and validate voltage
    parsed_value = None
    if value is not None:
        try:
            parsed_value = float(value)
        except ValueError:
            click.secho(f"Error: '{value}' is not a valid number", fg='red', err=True)
            ctx.exit(1)
        if parsed_value < 0:
            click.secho(f"Error: Battery empty voltage must be positive, got {parsed_value} V", fg='red', err=True)
            ctx.exit(1)

    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'set_volt_empty', netname=netname, value=parsed_value)


@battery.command()
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def capacity(ctx, box, value):
    """Set (or read) battery capacity limit in amp-hours (Ah)"""
    # Parse and validate capacity
    parsed_value = None
    if value is not None:
        try:
            parsed_value = float(value)
        except ValueError:
            click.secho(f"Error: '{value}' is not a valid number", fg='red', err=True)
            ctx.exit(1)
        if parsed_value <= 0:
            click.secho(f"Error: Capacity must be positive, got {parsed_value} Ah", fg='red', err=True)
            ctx.exit(1)

    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'set_capacity', netname=netname, value=parsed_value)


@battery.command(name='current-limit')
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def current_limit(ctx, box, value):
    """Set (or read) maximum charge/discharge current in amps (A)"""
    # Keithley 2281S max current is 6A
    MAX_CURRENT = 6.0

    # Parse and validate current limit
    parsed_value = None
    if value is not None:
        try:
            parsed_value = float(value)
        except ValueError:
            click.secho(f"Error: '{value}' is not a valid number", fg='red', err=True)
            ctx.exit(1)
        if parsed_value < 0:
            click.secho(f"Error: Current limit must be positive, got {parsed_value} A", fg='red', err=True)
            ctx.exit(1)
        if parsed_value > MAX_CURRENT:
            click.secho(f"Error: Current limit must not exceed {MAX_CURRENT} A, got {parsed_value} A", fg='red', err=True)
            ctx.exit(1)

    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'set_current_limit', netname=netname, value=parsed_value)


@battery.command()
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def ovp(ctx, box, value):
    """Set (or read) over voltage protection limit in volts (V)"""
    # Parse and validate OVP
    parsed_value = None
    if value is not None:
        try:
            parsed_value = float(value)
        except ValueError:
            click.secho(f"Error: '{value}' is not a valid number", fg='red', err=True)
            ctx.exit(1)
        if parsed_value <= 0:
            click.secho(f"Error: OVP must be positive, got {parsed_value} V", fg='red', err=True)
            ctx.exit(1)

    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'set_ovp', netname=netname, value=parsed_value)


@battery.command()
@click.argument('VALUE', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def ocp(ctx, box, value):
    """Set (or read) over current protection limit in amps (A)"""
    # Parse and validate OCP
    parsed_value = None
    if value is not None:
        try:
            parsed_value = float(value)
        except ValueError:
            click.secho(f"Error: '{value}' is not a valid number", fg='red', err=True)
            ctx.exit(1)
        if parsed_value <= 0:
            click.secho(f"Error: OCP must be positive, got {parsed_value} A", fg='red', err=True)
            ctx.exit(1)

    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'set_ocp', netname=netname, value=parsed_value)


@battery.command()
@click.argument('PARTNUMBER', required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def model(ctx, box, partnumber):
    """Set (or read) battery model (18650, nimh, lead-acid, etc.)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'set_model', netname=netname, partnumber=partnumber)


@battery.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def models(ctx, box):
    """List battery models saved on the instrument"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'list_models', netname=netname)


@battery.command(name='model-create')
@click.argument('SLOT', type=click.IntRange(1, 9))
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--csv', 'csv_path', required=True,
              type=click.Path(exists=True, dir_okay=False),
              help='CSV curve file: voc,resistance columns (header optional), '
                   'exactly 11 or 101 rows')
@click.option('--force', is_flag=True,
              help='Overwrite the slot if it already holds a model')
def model_create(ctx, box, slot, csv_path, force):
    """Create a custom battery model in a memory slot from a CSV file

    The CSV has two columns, open-circuit voltage (V) and internal
    resistance (Ω), ordered from empty to full: VOC non-decreasing,
    resistance non-increasing. 11-row files are interpolated to 101 points
    on the instrument. Saving overwrites the slot, and slots cannot be
    emptied afterwards (the instrument has no delete) — only overwritten.
    """
    try:
        voc, resistance = parse_model_csv(csv_path)
    except ValueError as exc:
        raise LagerError(
            "Battery model CSV validation failed.",
            cause=str(exc),
            fixes=[f"lager battery [NET_NAME] model-export {slot} --csv example.csv  "
                   "(an existing slot makes a good template)"],
        )

    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")

    if not force:
        # Occupancy guard: saving silently overwrites and there is no way to
        # empty a slot afterwards, so refuse occupied slots without --force.
        catalog = _battery_command_request(
            ctx, resolved_box, 'list_models', netname=netname)
        if any(entry.get('slot') == slot for entry in catalog.get('models', [])):
            raise LagerError(
                f"Battery model slot {slot} already holds a model.",
                cause="Saving would silently overwrite it, and the instrument "
                      "has no way to delete a model — only overwrite it.",
                fixes=[
                    f"lager battery {netname} model-create {slot} --csv {csv_path} --force",
                    f"lager battery {netname} models   (find a free slot)",
                ],
            )

    _run_backend(ctx, resolved_box, 'create_model', netname=netname,
                 slot=slot, voc=voc, resistance=resistance)
    click.echo(f"Load it with 'lager battery {netname} model {slot}'; "
               f"'models' lists all slots.")


@battery.command(name='model-export')
@click.argument('SLOT', type=click.IntRange(1, 9))
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--csv', 'csv_path', required=True, type=click.Path(dir_okay=False),
              help='Output CSV file to write')
def model_export(ctx, box, slot, csv_path):
    """Export a saved battery model's curve to a CSV file

    Writes the slot's 101 voc,resistance points in the format model-create
    accepts, for the export → edit → create round-trip. Read-only: exporting
    does not change the active model.
    """
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    result = _battery_command_request(
        ctx, resolved_box, 'export_model', netname=netname, slot=slot)
    model = result.get('model') or {}
    points = model.get('points') or []
    if not points:
        raise LagerError(
            f"The box returned no curve points for battery model slot {slot}.",
            cause="Its Lager software may predate the model-export command.",
            fixes=[f"lager update --box {resolved_box}"],
        )
    write_model_csv(csv_path, points)
    click.echo(f"\033[92m{result.get('message', 'Model exported')}\033[0m")
    click.echo(f"Wrote {len(points)} points to {csv_path}")


@battery.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def state(ctx, box):
    """Get battery state (comprehensive status)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'print_state', netname=netname)


@battery.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--yes', is_flag=True, help='Confirm the action without prompting.')
def enable(ctx, box, yes):
    """Enable battery simulator output"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")

    if yes or click.confirm(f"Enable Net?", default=False):
        pass
    else:
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

    if yes or click.confirm(f"Disable Net?", default=True):
        pass
    else:
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
    _run_backend(ctx, resolved_box, 'clear', netname=netname)


@battery.command(name='clear-ovp')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def clear_ovp(ctx, box):
    """Clear OVP trip condition"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'clear_ovp', netname=netname)


@battery.command(name='clear-ocp')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def clear_ocp(ctx, box):
    """Clear OCP trip condition"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")
    _run_backend(ctx, resolved_box, 'clear_ocp', netname=netname)


@battery.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def tui(ctx, box):
    """Launch interactive battery control TUI"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "battery")

    if not validate_net(ctx, resolved_box, netname, BATTERY_ROLE):
        # Match the supply TUI's behavior: red error on stderr, exit 1
        # (this used to print to stdout and exit 0, so scripts/CI could
        # not detect the failure).
        click.secho(f"Error: '{netname}' is not a battery net", fg='red', err=True)
        ctx.exit(1)

    try:
        # Import from the original battery location for TUI
        from ...battery.battery_tui import BatteryTUI
        app = BatteryTUI(ctx, netname, resolved_box, resolved_box)
        asyncio.run(app.run_async())
    except Exception:
        raise
