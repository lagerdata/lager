# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Watt meter commands for power / current / voltage measurement.
"""
from __future__ import annotations

import json

import click
from ...context import get_default_net, get_impl_path
from ..development.python import run_python_internal
from ...core.net_group import NetGroup
from ...core.net_helpers import (
    resolve_box,
    display_nets,
    validate_net_exists,
)

WATT_ROLE = "watt-meter"

# Base timeout for watt meter readings (seconds); scaled up for long durations.
WATT_TIMEOUT = 30


class WattGroup(NetGroup):
    """NetGroup that defaults to the ``power`` subcommand.

    Preserves the original ``lager watt NET [--box X]`` (read power) form. A
    plain Click group stops parsing options at the first positional, so a
    ``--box`` placed *after* ``NET`` would be misread as a subcommand name. When
    a net name is given with no explicit subcommand, we inject ``power`` right
    after it, so power reads (and their ``--box`` / ``--duration`` / ``--json``
    options) flow through the same subcommand machinery as current/voltage/all.
    """

    DEFAULT_CMD = "power"

    def parse_args(self, ctx, args):
        if (args and not args[0].startswith("-")
                and args[0] not in self.commands
                and "--help" not in args and "-h" not in args):
            # Leading NET_NAME present; ensure a subcommand follows it.
            if len(args) < 2 or args[1] not in self.commands:
                args = [args[0], self.DEFAULT_CMD, *args[1:]]
        return super().parse_args(ctx, args)


def _require_netname(ctx):
    """Get the netname stashed on ctx.obj by the group, or fall back to default."""
    netname = getattr(ctx.obj, "watt_netname", None)
    if netname is None:
        netname = get_default_net(ctx, 'watt')
    return netname


def _run_watt(ctx, box, netname, mode, duration, as_json):
    """Shared implementation for the power/current/voltage/all reads."""
    # Honor a group-level --box (e.g. `lager watt --box X NET current`) when the
    # subcommand itself didn't receive one.
    if box is None:
        box = getattr(ctx.obj, "watt_box", None)

    box_ip = resolve_box(ctx, box)

    # No net resolved (and none configured) -> list available watt meter nets.
    if netname is None:
        display_nets(ctx, box_ip, None, WATT_ROLE, "watt meter")
        return

    netname = netname.strip()

    # Validate net exists before executing command
    net = validate_net_exists(ctx, box_ip, netname, WATT_ROLE)
    if net is None:
        return  # Error already displayed

    payload = json.dumps({
        "netname": netname,
        "mode": mode,
        "duration": duration,
        "json": as_json,
    })

    # Give the box enough time for long averaging windows.
    timeout = max(WATT_TIMEOUT, int(duration) + 20)

    try:
        run_python_internal(
            ctx=ctx,
            runnable=get_impl_path("watt.py"),
            box=box_ip,
            env=(),
            passenv=(),
            kill=False,
            download=(),
            allow_overwrite=False,
            signum="SIGTERM",
            timeout=timeout,
            detach=False,
            port=(),
            org=None,
            args=[payload],
        )
    except SystemExit as e:
        # Re-raise non-zero exits to preserve exit code
        if e.code != 0:
            raise
    except Exception as e:
        error_str = str(e)
        click.secho("Error: Failed to read watt meter", fg='red', err=True)
        if "Connection refused" in error_str:
            click.secho(f"Could not connect to box at {box_ip}", err=True)
            click.secho("Check that the box is online and Docker container is running.", err=True)
        elif "timed out" in error_str.lower():
            click.secho("Watt meter reading timed out.", err=True)
            click.secho("Possible causes:", err=True)
            click.secho("  - Watt meter not connected or powered off", err=True)
            click.secho("  - USB connection issue", err=True)
            click.secho("  - Device at incorrect address", err=True)
        elif "device not found" in error_str.lower() or "no such device" in error_str.lower():
            click.secho("Watt meter device not found.", err=True)
            click.secho("Check that the watt meter (Yocto-Watt or Joulescope JS220) is connected via USB.", err=True)
        else:
            click.secho(f"Details: {e}", err=True)
        ctx.exit(1)


# Shared options for the read subcommands.
def _read_options(func):
    func = click.option("--box", required=False, help="Lagerbox name or IP")(func)
    func = click.option(
        "--duration", "-d", type=float, default=0.1, show_default=True,
        help="Averaging window in seconds (longer = lower noise / higher resolution)",
    )(func)
    func = click.option(
        "--json", "as_json", is_flag=True, default=False,
        help="Emit a machine-readable JSON object instead of formatted text",
    )(func)
    return func


@click.group(
    name="watt",
    cls=WattGroup,
    invoke_without_command=True,
    help="Read power/current/voltage from a watt-meter net",
)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.argument("netname", required=False, metavar="[NET_NAME]")
@click.pass_context
def watt(ctx, box, netname):
    """Watt meter group.  Usage: lager watt [NET_NAME] [COMMAND] --box [BOX_NAME]

    With no subcommand, reads power (watts). Use the current/voltage/all
    subcommands for other quantities.
    """
    if netname is None:
        netname = get_default_net(ctx, 'watt')
    if netname is not None:
        ctx.obj.watt_netname = netname
    ctx.obj.watt_box = box

    if ctx.invoked_subcommand is None:
        if netname is not None:
            # A net was named (explicitly, or via a configured default) but no
            # subcommand followed -> read power, matching `lager watt NET`. This
            # covers option-first ordering (`lager watt --box X NET`) and default
            # nets, which don't go through WattGroup's power-subcommand injection.
            _run_watt(ctx, box, netname, "power", 0.1, False)
        else:
            # No net resolved -> list available watt meter nets.
            box_ip = resolve_box(ctx, box)
            display_nets(ctx, box_ip, None, WATT_ROLE, "watt meter")


watt.net_examples = [
    "lager watt watt1 --box <BOX>                 (read power)",
    "lager watt watt1 current --box <BOX>         (read current)",
    "lager watt watt1 voltage --box <BOX>         (read voltage)",
    "lager watt watt1 all --box <BOX>             (read I, V, and P)",
    "lager watt watt1 all --duration 1.0 --json --box <BOX>",
    "lager watt --box <BOX>                       (list watt meter nets)",
]


@watt.command(name="power", help="Read power in watts")
@_read_options
@click.pass_context
def power(ctx, box, duration, as_json):
    _run_watt(ctx, box, _require_netname(ctx), "power", duration, as_json)


@watt.command(name="current", help="Read current in amps (Joulescope/PPK2)")
@_read_options
@click.pass_context
def current(ctx, box, duration, as_json):
    _run_watt(ctx, box, _require_netname(ctx), "current", duration, as_json)


@watt.command(name="voltage", help="Read voltage in volts (Joulescope/PPK2)")
@_read_options
@click.pass_context
def voltage(ctx, box, duration, as_json):
    _run_watt(ctx, box, _require_netname(ctx), "voltage", duration, as_json)


@watt.command(name="all", help="Read current, voltage, and power together (Joulescope/PPK2)")
@_read_options
@click.pass_context
def all_(ctx, box, duration, as_json):
    _run_watt(ctx, box, _require_netname(ctx), "all", duration, as_json)
