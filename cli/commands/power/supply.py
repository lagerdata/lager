# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    Supply commands (local nets; Rigol DP800 friendly)

    Usage:
      lager supply                    -> lists only supply nets
      lager supply <NETNAME> voltage  -> set/read voltage on that net
      lager supply <NETNAME> current  -> set/read current on that net
      lager supply <NETNAME> enable
      lager supply <NETNAME> disable
      lager supply <NETNAME> state
      lager supply <NETNAME> clear-ocp
      lager supply <NETNAME> clear-ovp
      lager supply <NETNAME> set
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
import asyncio

import click

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
    NET_ROLES,
)
from ...context import get_default_box, get_impl_path, get_default_net
from ...options import force_command_option
from ..development.python import run_python_internal


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
    """
    Run backend command and handle errors gracefully.

    First tries to use the WebSocket HTTP endpoint if a TUI is running for this net,
    which allows sharing the USB connection. Falls back to direct access if no TUI is active.
    """
    import requests

    netname = getattr(ctx.obj, "netname", None)

    # Try WebSocket HTTP endpoint first (for concurrent TUI + CLI access)
    if netname:
        try:
            # Get box IP
            from ...box_storage import resolve_and_validate_box
            box_ip = resolve_and_validate_box(ctx, box)

            # Try the WebSocket-shared endpoint
            url = f"http://{box_ip}:9000/supply/command"
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
                    click.echo(f"[OK] {message}")
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
        "action": action,
        "params": params,
    }

    # Capture stderr to detect Resource busy errors
    stderr_capture = io.StringIO()

    try:
        with redirect_stderr(stderr_capture):
            run_python_internal(
                ctx,
                get_impl_path("supply.py"),
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
    except SystemExit as e:
        # Get captured stderr
        stderr_output = stderr_capture.getvalue()

        # Check if this is a "Resource busy" error
        if e.code != 0 and "Resource busy" in stderr_output:
            click.echo(stderr_output, err=True)  # Print the original error
            click.echo("\n" + "="*70, err=True)
            click.echo("WARNING: Power supply is currently in use by the TUI", err=True)
            click.echo("="*70, err=True)
            click.echo(f"\nThe power supply '{netname}' cannot be accessed because it's being used", err=True)
            click.echo("by an active 'lager supply tui' session.\n", err=True)
            click.echo("To fix this:", err=True)
            click.echo(f"  1. Close the TUI: Press 'q' or 'Ctrl+C' in the TUI window", err=True)
            click.echo(f"  2. Then retry this command", err=True)
            click.echo("\nOr use the TUI's command prompt to control the supply interactively.", err=True)
            raise SystemExit(1)
        elif e.code != 0:
            # Other error - print captured stderr and re-raise
            click.echo(stderr_output, err=True)
            raise

    # Provide feedback for operations that don't naturally produce output
    if action in ["set_mode", "clear_ovp", "clear_ocp", "enable", "disable"]:
        operation_names = {
            "set_mode": "Set power supply mode",
            "clear_ovp": "Cleared OVP protection",
            "clear_ocp": "Cleared OCP protection",
            "enable": "Enabled supply output",
            "disable": "Disabled supply output"
        }
        click.echo(f"[OK] {operation_names.get(action, 'Operation completed')}")

    # Provide feedback for voltage/current set operations
    if action == "voltage" and params.get("value") is not None:
        click.echo(f"[OK] Voltage set to {params.get('value')}V")
    elif action == "current" and params.get("value") is not None:
        click.echo(f"[OK] Current set to {params.get('value')}A")


# ---------- CLI ----------

@click.group(invoke_without_command=True)
@click.argument("NETNAME", required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@force_command_option
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
