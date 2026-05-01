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
from ...output import ExitCode, action as output_action, error as output_error
from ..development.python import run_python_internal


SUPPLY_ROLE = NET_ROLES["power_supply"]  # "power-supply"

# Typical power supply limits (conservative, works for most bench supplies)
MAX_VOLTAGE = 100.0   # Volts - most bench supplies are <=60V, but some go higher
MAX_CURRENT = 30.0    # Amps - typical bench supply limit
MAX_OVP = 110.0       # OVP can be slightly higher than max output
MAX_OCP = 33.0        # OCP can be slightly higher than max output


def _validate_supply_limits(ctx, voltage=None, current=None, ovp=None, ocp=None):
    """Validate power supply values are within safe limits."""
    cfg = ctx.obj.output
    if voltage is not None and voltage > MAX_VOLTAGE:
        output_error(
            f"Voltage {voltage}V exceeds maximum limit of {MAX_VOLTAGE}V "
            "(most bench supplies are limited to 30-60V; check equipment specs).",
            cfg=cfg, exit_code=ExitCode.USER_ERROR,
            command="supply.voltage",
            data={"voltage": voltage, "limit": MAX_VOLTAGE},
        )

    if current is not None and current > MAX_CURRENT:
        output_error(
            f"Current {current}A exceeds maximum limit of {MAX_CURRENT}A "
            "(most bench supplies are limited to 3-10A; check equipment specs).",
            cfg=cfg, exit_code=ExitCode.USER_ERROR,
            command="supply.current",
            data={"current": current, "limit": MAX_CURRENT},
        )

    if ovp is not None and ovp > MAX_OVP:
        output_error(
            f"OVP {ovp}V exceeds maximum limit of {MAX_OVP}V",
            cfg=cfg, exit_code=ExitCode.USER_ERROR,
            command="supply.ovp", data={"ovp": ovp, "limit": MAX_OVP},
        )

    if ocp is not None and ocp > MAX_OCP:
        output_error(
            f"OCP {ocp}A exceeds maximum limit of {MAX_OCP}A",
            cfg=cfg, exit_code=ExitCode.USER_ERROR,
            command="supply.ocp", data={"ocp": ocp, "limit": MAX_OCP},
        )


# ---------- Supply-specific backend runner ----------

def _backend_env(ctx) -> tuple[str, ...]:
    """Build the env-var tuple to forward to the impl script.

    Always includes LAGER_OUTPUT_FORMAT and LAGER_OUTPUT_COLOR so the box-side
    cli_output helper renders consistently with the host-side cfg.
    """
    cfg = ctx.obj.output
    return (
        f"LAGER_OUTPUT_FORMAT={cfg.format.value}",
        f"LAGER_OUTPUT_COLOR={'1' if cfg.color else '0'}",
    )


def _run_backend(ctx, box, action: str, **params):
    """
    Run backend command and handle errors gracefully.

    First tries to use the WebSocket HTTP endpoint if a TUI is running for this net,
    which allows sharing the USB connection. Falls back to direct access if no TUI is active.
    """
    import requests

    cfg = ctx.obj.output
    netname = getattr(ctx.obj, "netname", None)
    subject = {"net": netname} if netname else None

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
                    output_action(message, cfg=cfg, command=f"supply.{action}", subject=subject)
                    return
                else:
                    output_error(
                        result.get('error', 'Unknown error'),
                        cfg=cfg, exit_code=ExitCode.BACKEND_ERROR,
                        command=f"supply.{action}", subject=subject,
                    )

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
                env=(
                    f"LAGER_COMMAND_DATA={json.dumps(data)}",
                    *_backend_env(ctx),
                ),
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

        # Check if this is a "Resource busy" error — re-route through output.error
        # so JSON consumers get a structured envelope.
        if e.code != 0 and "Resource busy" in stderr_output:
            output_error(
                f"Power supply '{netname}' is currently in use by the TUI. "
                "Close the TUI ('q' or Ctrl+C) and retry, or use the TUI's command prompt.",
                cfg=cfg, exit_code=ExitCode.BACKEND_ERROR,
                command=f"supply.{action}", subject=subject,
                data={"reason": "resource_busy", "tui_active": True,
                      "raw_stderr": stderr_output.strip()},
            )
        elif e.code != 0:
            # Forward the raw stderr (preserves the impl-script error envelope
            # in JSON mode; preserves the colored stderr block in text mode).
            click.echo(stderr_output, err=True)
            raise

    # Provide feedback for operations that don't naturally produce output.
    # output_action handles both text and JSON modes — the impl script is
    # silent for these actions, so this is the only ack emitted.
    if action in ["set_mode", "clear_ovp", "clear_ocp", "enable", "disable"]:
        operation_names = {
            "set_mode": "Set power supply mode",
            "clear_ovp": "Cleared OVP protection",
            "clear_ocp": "Cleared OCP protection",
            "enable": "Enabled supply output",
            "disable": "Disabled supply output",
        }
        output_action(operation_names.get(action, "Operation completed"),
                      cfg=cfg, command=f"supply.{action}", subject=subject)
        return

    if action == "voltage" and params.get("value") is not None:
        output_action(f"Voltage set to {params.get('value')} V",
                      cfg=cfg, command="supply.voltage", subject=subject)
    elif action == "current" and params.get("value") is not None:
        output_action(f"Current set to {params.get('value')} A",
                      cfg=cfg, command="supply.current", subject=subject)


# ---------- CLI ----------

@click.group(invoke_without_command=True)
@click.argument("NETNAME", required=False)
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
    if validate_net_exists(ctx, resolved_box, netname, SUPPLY_ROLE) is None:
        return
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
    if validate_net_exists(ctx, resolved_box, netname, SUPPLY_ROLE) is None:
        return
    _run_backend(ctx, resolved_box, action="enable", netname=netname)


@supply.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def state(ctx, box):
    """Read power state"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")
    if validate_net_exists(ctx, resolved_box, netname, SUPPLY_ROLE) is None:
        return
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
    if validate_net_exists(ctx, resolved_box, netname, SUPPLY_ROLE) is None:
        return
    _run_backend(ctx, resolved_box, action="set_mode", netname=netname)


@supply.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def clear_ovp(ctx, box):
    """Clear over-voltage protection trip condition"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")
    if validate_net_exists(ctx, resolved_box, netname, SUPPLY_ROLE) is None:
        return
    _run_backend(ctx, resolved_box, action="clear_ovp", netname=netname)


@supply.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def clear_ocp(ctx, box):
    """Clear over-current protection trip condition"""
    resolved_box = resolve_box(ctx, box)
    netname = require_netname(ctx, "supply")
    if validate_net_exists(ctx, resolved_box, netname, SUPPLY_ROLE) is None:
        return
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
