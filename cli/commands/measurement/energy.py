# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Energy analyzer commands.
"""
from __future__ import annotations

import click
from ...context import get_default_net
from ...core.net_group import NetGroup
from ...core.net_helpers import (
    resolve_box,
    display_nets,
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


def _run_energy(ctx, box, duration, netname, mode):
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
    action = "read_energy" if mode == "energy" else "read_stats"
    post_net_command(ctx, box_ip, netname, action, role="energy-analyzer",
                     duration=duration)


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
@click.pass_context
def read_energy(ctx, box, duration):
    netname = _require_netname(ctx)
    _run_energy(ctx, box, duration, netname, "energy")


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
@click.pass_context
def stats(ctx, box, duration):
    netname = _require_netname(ctx)
    _run_energy(ctx, box, duration, netname, "stats")
