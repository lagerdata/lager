# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Energy analyzer commands.
"""
from __future__ import annotations

import json

import click
from ...context import get_default_net, get_impl_path
from ..development.python import run_python_internal
from ...core.net_helpers import (
    resolve_box,
    display_nets,
    validate_net_exists,
)

ENERGY_ROLE = "energy-analyzer"
ENERGY_TIMEOUT = 120  # allow up to 2 min for long integrations


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

    payload = json.dumps({"netname": netname, "duration": duration, "mode": mode})

    try:
        run_python_internal(
            ctx=ctx,
            runnable=get_impl_path("energy.py"),
            box=box_ip,
            env=(),
            passenv=(),
            kill=False,
            download=(),
            allow_overwrite=False,
            signum="SIGTERM",
            timeout=ENERGY_TIMEOUT,
            detach=False,
            port=(),
            org=None,
            args=[payload],
        )
    except SystemExit as e:
        if e.code != 0:
            raise
    except Exception as e:
        click.secho(f"Error: Failed to read energy {mode}", fg='red', err=True)
        click.secho(f"Details: {e}", err=True)
        ctx.exit(1)


@click.group(
    name="energy",
    invoke_without_command=True,
    help="Read energy/charge from an energy-analyzer net",
)
@click.argument("netname", required=False)
@click.pass_context
def energy(ctx, netname):
    """Energy analyzer group.  Usage: lager energy <NETNAME> [read|stats] [OPTIONS]"""
    if netname is None:
        netname = get_default_net(ctx, 'energy')
    if netname is not None:
        ctx.obj.energy_netname = netname

    if ctx.invoked_subcommand is None:
        # No subcommand → list nets
        box_ip = resolve_box(ctx, None)
        display_nets(ctx, box_ip, None, ENERGY_ROLE, "energy analyzer")


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
