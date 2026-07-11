# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Solar simulator CLI commands.

Usage:
    lager solar                         -> lists solar nets
    lager solar [NET_NAME] irradiance 1000  -> set irradiance to 1000 W/m²
    lager solar [NET_NAME] set           -> initialize solar mode
    lager solar [NET_NAME] stop          -> stop solar mode
"""
from __future__ import annotations

import click

from ...core.net_group import NetGroup
# Import consolidated helpers from cli.core.net_helpers
from ...core.net_helpers import (
    resolve_box,
    display_nets,
    post_net_command,
    NET_ROLES,
)
from ...context import get_default_net
from ...box_storage import resolve_and_validate_box


SOLAR_ROLE = NET_ROLES["solar"]  # "solar"

# Solar simulator limits (typical for EA PSI/EL series)
MAX_IRRADIANCE = 1500.0  # W/m² - typical max for solar simulators
MIN_RESISTANCE = 0.1    # Ohms - minimum dynamic panel resistance
MAX_RESISTANCE = 100.0  # Ohms - maximum dynamic panel resistance


# ---------- Solar backend runner (:9000 /net/command) ----------

def _run_backend(ctx: click.Context, box: str | None, action: str, **params) -> None:
    """Send action (+ params) for the current solar net via :9000 /net/command.

    Runs in-process on the box through hardware_service, so a solar net and a
    supply net on the same EA share one per-VISA-address lock. There is no
    :5000 script-upload fallback.
    """
    # Retrieve the net name from context (set by the group callback)
    netname = getattr(ctx.obj, "netname", None)
    if not netname:
        raise click.UsageError("NETNAME required for solar command.")

    # Determine target box (if any)
    if box is None:
        box = ctx.obj.box

    # Resolve and validate the box name
    resolved_box = resolve_and_validate_box(ctx, box)

    # Every solar action re-asserts PV mode on the instrument (enable() with
    # settle retries), so widen the HTTP budget past the box-side proxy
    # timeout (90s set/stop, 60s reads).
    http_timeout = 120 if action in ("set", "stop") else 90
    result = post_net_command(ctx, resolved_box, netname, action, role=SOLAR_ROLE,
                              quiet=True, http_timeout=http_timeout, **params)
    message = result.get("message")
    if message:
        click.secho(message, fg="green")


# ---------- CLI ----------

@click.group(cls=NetGroup, invoke_without_command=True, help="Control solar simulator settings and output")
@click.argument("netname", required=False, metavar="[NET_NAME]")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def solar(ctx, netname, box):
    """
    Top-level solar command: stores net & optional box.
    """
    if ctx.obj is None:
        # Create a simple attribute container for storing context data
        class _Obj:
            pass
        ctx.obj = _Obj()

    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'solar')

    # Store provided net name and box (if any) in context
    setattr(ctx.obj, "netname", netname)
    setattr(ctx.obj, "box", box)

    # If no subcommand and no netname, list nets
    if ctx.invoked_subcommand is None:
        resolved_box = resolve_box(ctx, box)
        display_nets(ctx, resolved_box, None, SOLAR_ROLE, "solar")


solar.net_examples = [
    "lager solar solar1 set --box <BOX>",
    "lager solar solar1 irradiance 1000 --box <BOX>",
    "lager solar solar1 voc --box <BOX>",
    "lager solar --box <BOX>                (list solar nets)",
]


@solar.command("set", help="Initialize and start solar simulation mode")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def set_mode(ctx: click.Context, box: str | None) -> None:
    """Initialize and start the solar simulation mode."""
    _run_backend(ctx, box, "set")


@solar.command("stop", help="Stop solar simulation mode")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def stop_mode(ctx: click.Context, box: str | None) -> None:
    """Stop the solar simulation mode."""
    _run_backend(ctx, box, "stop")


def _irradiance_range_callback(ctx, param, value):
    """Custom callback for irradiance validation with better error messages."""
    if value is not None and (value < 0.0 or value > MAX_IRRADIANCE):
        raise click.BadParameter(
            f"Irradiance must be between 0 and {MAX_IRRADIANCE} W/m², got {value}\n"
            f"  0 W/m² = darkness (no sunlight)\n"
            f"  1000 W/m² = standard test condition (AM1.5)\n"
            f"  {MAX_IRRADIANCE} W/m² = maximum simulator capability"
        )
    return value


@solar.command("irradiance", help="Set (or read) irradiance in watts per square meter (W/m²)")
@click.argument("value", required=False, type=float, callback=_irradiance_range_callback)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def irradiance(ctx: click.Context, value: float | None, box: str | None) -> None:
    """Get or set the irradiance in W/m²."""
    if value is None:
        _run_backend(ctx, box, "irradiance")
    else:
        _run_backend(ctx, box, "irradiance", value=value)


@solar.command("mpp-current", help="Read maximum power point current in amps (A)")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def mpp_current(ctx: click.Context, box: str | None) -> None:
    """Return the MPP current (A)."""
    _run_backend(ctx, box, "mpp_current")


@solar.command("mpp-voltage", help="Read maximum power point voltage in volts (V)")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def mpp_voltage(ctx: click.Context, box: str | None) -> None:
    """Return the MPP voltage (V)."""
    _run_backend(ctx, box, "mpp_voltage")


def _resistance_range_callback(ctx, param, value):
    """Custom callback for resistance validation with better error messages."""
    if value is not None and (value < MIN_RESISTANCE or value > MAX_RESISTANCE):
        raise click.BadParameter(
            f"Resistance must be between {MIN_RESISTANCE} and {MAX_RESISTANCE} ohms, got {value}\n"
            f"  {MIN_RESISTANCE} ohms = minimum dynamic resistance (high-efficiency panel)\n"
            f"  1-10 ohms = typical range for most panels\n"
            f"  {MAX_RESISTANCE} ohms = maximum dynamic resistance"
        )
    return value


@solar.command("resistance", help="Set (or read) dynamic panel resistance in ohms")
@click.argument("value", required=False, type=float, callback=_resistance_range_callback)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def resistance(ctx: click.Context, value: float | None, box: str | None) -> None:
    """Get or set the dynamic panel resistance (ohms)."""
    if value is None:
        _run_backend(ctx, box, "resistance")
    else:
        _run_backend(ctx, box, "resistance", value=value)


@solar.command("temperature", help="Read cell temperature in degrees Celsius")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def temperature(ctx: click.Context, box: str | None) -> None:
    """Return the cell temperature (degrees C)."""
    _run_backend(ctx, box, "temperature")


@solar.command("voc", help="Read open-circuit voltage in volts (V)")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def voc(ctx: click.Context, box: str | None) -> None:
    """Return the open-circuit voltage (Voc)."""
    _run_backend(ctx, box, "voc")
