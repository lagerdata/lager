# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Energy analyzer commands.
"""
from __future__ import annotations

import json

import click
from ...context import get_default_net
from ...core.net_group import NetGroup
from ...core.net_helpers import (
    resolve_box,
    display_nets,
    fmt_si,
    post_net_command,
    validate_net_exists,
)

ENERGY_ROLE = "energy-analyzer"


def _require_netname(ctx):
    """Get the netname stored on ctx.obj by the group, or fall back to default."""
    netname = getattr(ctx.obj, "energy_netname", None)
    if netname is None:
        netname = get_default_net(ctx, 'energy')
    return netname


def _print_energy(netname, result):
    """Full energy breakdown (J/Wh, C/Ah) — matches the pre-:9000 output."""
    dur = result["duration_s"]
    click.secho(f"Energy '{netname}' ({dur:.1f}s integration):", fg="green")
    click.echo(f"  Energy:  {fmt_si(result['energy_j'], 'J')}  "
               f"({fmt_si(result['energy_wh'], 'Wh')})")
    click.echo(f"  Charge:  {fmt_si(result['charge_c'], 'C')}  "
               f"({fmt_si(result['charge_ah'], 'Ah')})")


def _print_stats(netname, result):
    """Per-quantity mean/min/max/std — matches the pre-:9000 output."""
    dur = result["duration_s"]
    click.secho(f"Stats '{netname}' ({dur:.1f}s):", fg="cyan")
    for label, key, unit in (("Current", "current", "A"),
                             ("Voltage", "voltage", "V"),
                             ("Power", "power", "W")):
        s = result[key]
        click.echo(f"  {label:<9s} mean={fmt_si(s['mean'], unit):<14s}"
                   f"min={fmt_si(s['min'], unit):<14s}"
                   f"max={fmt_si(s['max'], unit):<14s}"
                   f"std={fmt_si(s['std'], unit)}")


def _run_energy(ctx, box, duration, netname, mode, as_json=False):
    """Shared implementation for energy and stats commands."""
    box_ip = resolve_box(ctx, box)

    if netname is None:
        display_nets(ctx, box_ip, None, ENERGY_ROLE, "energy analyzer")
        return

    netname = netname.strip()

    net = validate_net_exists(ctx, box_ip, netname, ENERGY_ROLE)
    if net is None:
        return

    # mode "energy" -> integrate charge/energy; "stats" -> average I/V/P.
    # The box holds the request for the whole integration window, so give the
    # HTTP client duration + margin (matches the old :5000 path's 120s budget).
    action = "read_energy" if mode == "energy" else "read_stats"
    result = post_net_command(ctx, box_ip, netname, action,
                              role="energy-analyzer", quiet=True,
                              http_timeout=max(30.0, duration + 30.0),
                              duration=duration)
    value = result.get("value") or {}

    if as_json:
        click.echo(json.dumps({"netname": netname, **value}))
    elif mode == "energy":
        _print_energy(netname, value)
    else:
        _print_stats(netname, value)


@click.group(
    name="energy",
    cls=NetGroup,
    invoke_without_command=True,
    help="Read energy/charge from an energy-analyzer net",
)
@click.argument("netname", required=False, metavar="[NET_NAME]")
@click.pass_context
def energy(ctx, netname):
    """Energy analyzer group.  Usage: lager energy [NET_NAME] [COMMAND] --box [BOX_NAME]"""
    if netname is None:
        netname = get_default_net(ctx, 'energy')
    if netname is not None:
        ctx.obj.energy_netname = netname

    if ctx.invoked_subcommand is None:
        # No subcommand → list nets
        box_ip = resolve_box(ctx, None)
        display_nets(ctx, box_ip, None, ENERGY_ROLE, "energy analyzer")


energy.net_examples = [
    "lager energy energy1 read --duration 10 --box <BOX>",
    "lager energy energy1 stats --duration 5 --box <BOX>",
    "lager energy --box <BOX>               (list energy analyzer nets)",
]


@energy.command(
    name="read",
    help="Read energy/charge over a duration",
)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option(
    "--duration",
    type=float,
    default=10.0,
    show_default=True,
    help="Integration duration in seconds",
)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON object instead of formatted text")
@click.pass_context
def read_energy(ctx, box, duration, as_json):
    netname = _require_netname(ctx)
    _run_energy(ctx, box, duration, netname, "energy", as_json)


@energy.command(
    name="stats",
    help="Read current/voltage/power statistics over a duration",
)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option(
    "--duration",
    type=float,
    default=1.0,
    show_default=True,
    help="Measurement duration in seconds",
)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON object instead of formatted text")
@click.pass_context
def stats(ctx, box, duration, as_json):
    netname = _require_netname(ctx)
    _run_energy(ctx, box, duration, netname, "stats", as_json)
