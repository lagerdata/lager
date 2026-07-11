# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Watt meter commands for power / current / voltage measurement.
"""
from __future__ import annotations

import json

import click
from ...context import get_default_net
from ...core.net_group import NetGroup
from ...core.net_helpers import (
    resolve_box,
    display_nets,
    post_net_command,
    validate_net_exists,
)

WATT_ROLE = "watt-meter"

_UNITS = {"power": ("Power", "W"), "current": ("Current", "A"),
          "voltage": ("Voltage", "V")}


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

    # The box exposes each quantity as its own /net/command action; "all"
    # returns a {current, voltage, power} dict. post_net_command surfaces any
    # hardware error (e.g. UnsupportedInstrumentError for power-only meters).
    # The box holds the request for the whole averaging window, so give the
    # HTTP client duration + margin (matches the old :5000 path's budget).
    result = post_net_command(ctx, box_ip, netname, mode, role="watt-meter",
                              quiet=True, http_timeout=max(30.0, duration + 20.0),
                              duration=duration)
    value = result.get("value")

    if mode == "all":
        current = float(value["current"])
        voltage = float(value["voltage"])
        power = float(value["power"])
        if as_json:
            click.echo(json.dumps({
                "netname": netname, "current": current, "voltage": voltage,
                "power": power, "duration_s": duration,
            }))
        else:
            click.secho(f"Measurements '{netname}' ({duration:g}s):", fg="green")
            click.secho(f"  Current: {current:.6g} A", fg="green")
            click.secho(f"  Voltage: {voltage:.6g} V", fg="green")
            click.secho(f"  Power:   {power:.6g} W", fg="green")
        return

    v = float(value)
    label, unit = _UNITS[mode]
    if as_json:
        click.echo(json.dumps({"netname": netname, mode: v, "duration_s": duration}))
    else:
        click.secho(f"{label} '{netname}': {v:.6g} {unit}", fg="green")


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
