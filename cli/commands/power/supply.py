# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    Supply commands (local nets; Rigol DP800 friendly)

    Usage:
      lager supply                    -> lists only supply nets
      lager supply [NET_NAME] voltage  -> set/read voltage on that net
      lager supply [NET_NAME] current  -> set/read current on that net
      lager supply [NET_NAME] enable
      lager supply [NET_NAME] disable
      lager supply [NET_NAME] state
      lager supply [NET_NAME] clear-ocp
      lager supply [NET_NAME] clear-ovp
      lager supply [NET_NAME] set
"""
from __future__ import annotations

import asyncio

import click

from ...core.net_group import NetGroup
# Import consolidated helpers from cli.core.net_helpers
from ...core.net_helpers import (
    require_netname,
    resolve_box,
    validate_net,
    validate_net_exists,
    display_nets,
    validate_positive_parameters,
    validate_protection_limits,
    parse_value_with_negatives,
    NET_HTTP_PORT,
    NET_ROLES,
)
from ...context import get_default_net


SUPPLY_ROLE = NET_ROLES["power_supply"]  # "power-supply"

# Typical power supply limits (conservative, works for most bench supplies)
MAX_VOLTAGE = 100.0   # Volts - most bench supplies are <=60V, but some go higher
MAX_CURRENT = 30.0    # Amps - typical bench supply limit
MAX_OVP = 110.0       # OVP can be slightly higher than max output
MAX_OCP = 33.0        # OCP can be slightly higher than max output


def _validate_supply_limits(ctx, voltage=None, current=None, ovp=None, ocp=None):
    """Validate power supply values are within safe limits."""
    if voltage is not None and voltage > MAX_VOLTAGE:
        click.secho(
            f"Error: Voltage {voltage}V exceeds maximum limit of {MAX_VOLTAGE}V",
            fg='red', err=True
        )
        click.secho("  Most bench power supplies are limited to 30-60V.", err=True)
        click.secho("  Check your equipment specifications before proceeding.", err=True)
        ctx.exit(1)

    if current is not None and current > MAX_CURRENT:
        click.secho(
            f"Error: Current {current}A exceeds maximum limit of {MAX_CURRENT}A",
            fg='red', err=True
        )
        click.secho("  Most bench power supplies are limited to 3-10A.", err=True)
        click.secho("  Check your equipment specifications before proceeding.", err=True)
        ctx.exit(1)

    if ovp is not None and ovp > MAX_OVP:
        click.secho(
            f"Error: OVP {ovp}V exceeds maximum limit of {MAX_OVP}V",
            fg='red', err=True
        )
        ctx.exit(1)

    if ocp is not None and ocp > MAX_OCP:
        click.secho(
            f"Error: OCP {ocp}A exceeds maximum limit of {MAX_OCP}A",
            fg='red', err=True
        )
        ctx.exit(1)


# ---------- Supply-specific backend runner ----------

def _run_backend(ctx, box, action: str, **params):
    """Drive the power supply over the box's warm HTTP endpoint.

    Posts to :9000/supply/command, which shares the instrument connection with
    an active TUI session when one exists and otherwise drives the cached driver
    in-process. There is no :5000 script-upload fallback.

    ``box`` is the already-resolved box IP (callers pass resolve_box(...)).
    """
    import requests

    from ...gateway_auth import auth_headers_for_box
    from ...box_storage import _check_gateway

    netname = params.get("netname") or getattr(ctx.obj, "netname", None)
    url = f"http://{box}:{NET_HTTP_PORT}/supply/command"
    payload = {"netname": netname, "action": action, "params": params}

    try:
        response = requests.post(url, json=payload, timeout=10,
                                 headers=auth_headers_for_box(box))
        _check_gateway(response, box)
    except (requests.ConnectionError, requests.Timeout):
        click.secho(
            f"Error: cannot reach box at {box}:{NET_HTTP_PORT}. "
            f"Check network/Tailscale and that the box is online and updated.",
            fg='red', err=True,
        )
        raise SystemExit(1)
    except requests.RequestException as e:
        click.secho(f"Error: supply request to box failed: {e}", fg='red', err=True)
        raise SystemExit(1)

    try:
        result = response.json()
    except ValueError:
        click.secho(
            f"Error: box returned a non-JSON response (HTTP {response.status_code}).",
            fg='red', err=True,
        )
        raise SystemExit(1)

    if response.status_code == 200 and result.get('success'):
        click.echo(f"[OK] {result.get('message', 'Command executed')}")
        return

    error = result.get('error') or f"supply command failed (HTTP {response.status_code})"
    if response.status_code == 404:
        error = (f"{error}. This box image does not expose /supply/command; "
                 f"update the box.")
    click.secho(f"Error: {error}", fg='red', err=True)
    raise SystemExit(1)


# ---------- CLI ----------

@click.group(cls=NetGroup, invoke_without_command=True)
@click.argument("NETNAME", required=False, metavar="[NET_NAME]")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def supply(ctx, box, netname):
    """Control power supply voltage and current"""
    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'power_supply')

    if netname is not None:
        ctx.obj.netname = netname

    if ctx.invoked_subcommand is None:
        resolved_box = resolve_box(ctx, box)
        display_nets(ctx, resolved_box, None, SUPPLY_ROLE, "power supply")


supply.net_examples = [
    "lager supply supply1 voltage 3.3 --box <BOX>",
    "lager supply supply1 current 0.5 --box <BOX>",
    "lager supply supply1 enable --box <BOX>",
    "lager supply supply1 state --box <BOX>",
    "lager supply --box <BOX>               (list supply nets)",
]


@supply.command()
@click.argument("VALUE", required=False, callback=parse_value_with_negatives)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--ocp", required=False, type=click.FLOAT, help="Over-current protection limit in amps (A)")
@click.option("--ovp", required=False, type=click.FLOAT, help="Over-voltage protection limit in volts (V)")
@click.option("--yes", is_flag=True, default=False, help="Confirm the action without prompting")
def voltage(ctx, box, value, ocp, ovp, yes):
    """Set (or read) voltage in volts (V)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")

    # Validate net exists BEFORE prompting for confirmation
    if validate_net_exists(ctx, resolved_box, netname, SUPPLY_ROLE) is None:
        return  # Error already displayed

    # Validate positive values and protection limits at CLI level
    validate_positive_parameters(voltage=value, ocp=ocp, ovp=ovp)
    validate_protection_limits(voltage=value, ovp=ovp)

    # Validate upper bounds
    _validate_supply_limits(ctx, voltage=value, ovp=ovp, ocp=ocp)

    if value is not None and not (yes or click.confirm(f"Set voltage to {value} V?", default=False)):
        click.echo("Aborting")
        return

    _run_backend(
        ctx, resolved_box,
        action="voltage",
        netname=netname,
        value=value,
        ocp=ocp,
        ovp=ovp,
    )


@supply.command()
@click.argument("VALUE", required=False, callback=parse_value_with_negatives)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--ocp", required=False, type=click.FLOAT, help="Over-current protection limit in amps (A)")
@click.option("--ovp", required=False, type=click.FLOAT, help="Over-voltage protection limit in volts (V)")
@click.option("--yes", is_flag=True, default=False, help="Confirm the action without prompting")
def current(ctx, box, value, ocp, ovp, yes):
    """Set (or read) current in amps (A)"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")

    # Validate net exists BEFORE prompting for confirmation
    if validate_net_exists(ctx, resolved_box, netname, SUPPLY_ROLE) is None:
        return  # Error already displayed

    # Validate positive values and protection limits at CLI level
    validate_positive_parameters(current=value, ocp=ocp, ovp=ovp)
    validate_protection_limits(current=value, ocp=ocp)

    # Validate upper bounds
    _validate_supply_limits(ctx, current=value, ovp=ovp, ocp=ocp)

    if value is not None and not (yes or click.confirm(f"Set current to {value} A?", default=False)):
        click.echo("Aborting")
        return

    _run_backend(
        ctx, resolved_box,
        action="current",
        netname=netname,
        value=value,
        ocp=ocp,
        ovp=ovp,
    )


@supply.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--yes", is_flag=True, help="Confirm the action without prompting")
def disable(ctx, box, yes):
    """Disable supply output"""
    if not yes and not click.confirm("Disable Net?", default=False):
        click.echo("Aborting")
        return
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")
    _run_backend(ctx, resolved_box, action="disable", netname=netname)


@supply.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--yes", is_flag=True, help="Confirm the action without prompting")
def enable(ctx, box, yes):
    """Enable supply output"""
    if not yes and not click.confirm("Enable Net?", default=False):
        click.echo("Aborting")
        return
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")
    _run_backend(ctx, resolved_box, action="enable", netname=netname)


@supply.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def state(ctx, box):
    """Read power state"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")
    _run_backend(ctx, resolved_box, action="state", netname=netname)


@supply.command(name="set")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def set_mode(ctx, box):
    """
        Set power supply mode
    """
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")
    _run_backend(ctx, resolved_box, action="set_mode", netname=netname)


@supply.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def clear_ovp(ctx, box):
    """Clear over-voltage protection trip condition"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")
    _run_backend(ctx, resolved_box, action="clear_ovp", netname=netname)


@supply.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def clear_ocp(ctx, box):
    """Clear over-current protection trip condition"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")
    _run_backend(ctx, resolved_box, action="clear_ocp", netname=netname)


@supply.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def tui(ctx, box):
    """Launch interactive supply control TUI"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")

    if not validate_net(ctx, box, netname, SUPPLY_ROLE):
        click.secho(f"Error: '{netname}' is not a power supply net", fg='red', err=True)
        ctx.exit(1)

    try:
        # Import from the original supply location for TUI
        from ...supply.supply_tui import SupplyTUI
        app = SupplyTUI(ctx, netname, resolved_box, resolved_box)
        asyncio.run(app.run_async())
        # If the TUI exited because of an error, surface it to the user now —
        # the in-app log pane was inside the alt-screen and is no longer visible.
        exit_error = getattr(app, 'exit_error', None)
        if exit_error:
            click.secho(f"Error: {exit_error}", fg='red', err=True)
            ctx.exit(1)
    except ImportError as e:
        click.secho("Error: TUI dependencies not available", fg='red', err=True)
        click.secho(f"Missing module: {e.name if hasattr(e, 'name') else str(e)}", err=True)
        click.secho("Install with: pip install textual", err=True)
        ctx.exit(1)
    except ConnectionRefusedError:
        click.secho(f"Error: Cannot connect to box '{resolved_box}'", fg='red', err=True)
        click.secho("The box service may not be running.", err=True)
        click.secho(f"Check connectivity with: lager hello --box {box or resolved_box}", err=True)
        ctx.exit(1)
    except OSError as e:
        error_str = str(e).lower()
        if 'resource busy' in error_str or 'device or resource busy' in error_str:
            click.secho("Error: Power supply is already in use", fg='red', err=True)
            click.secho("Another TUI session or process may be using this supply.", err=True)
            click.secho("Close other sessions and try again.", err=True)
        elif 'no route to host' in error_str:
            click.secho(f"Error: Cannot reach box '{resolved_box}'", fg='red', err=True)
            click.secho("Check your VPN/Tailscale connection.", err=True)
        else:
            click.secho(f"Error: {e}", fg='red', err=True)
        ctx.exit(1)
    except KeyboardInterrupt:
        # User pressed Ctrl+C, exit gracefully
        click.echo("\nTUI closed.")
    except Exception as e:
        click.secho(f"Error: TUI failed: {e}", fg='red', err=True)
        click.secho("Troubleshooting tips:", err=True)
        click.secho(f"  1. Check box connectivity: lager hello --box {box or resolved_box}", err=True)
        click.secho(f"  2. Check the net exists: lager supply --box {box or resolved_box}", err=True)
        click.secho(f"  3. Check power supply is connected to box", err=True)
        ctx.exit(1)
