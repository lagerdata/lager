# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Bench digital multimeter (DMM) commands.

One command takes one reading: ``lager dmm NET [FUNCTION]`` with optional
range, NPLC and temperature sensor. The box validates the request against
the meter and answers an unsupported setting with an error instead of
reading with something close to it.
"""
from __future__ import annotations

import json

import click

from ...context import get_default_net
from ...core.net_group import NetCommand
from ...core.net_helpers import (
    display_nets,
    fetch_nets,
    post_net_command,
    resolve_box_locked,
    validate_net_exists,
)

DMM_ROLE = "dmm"

# Same tokens as box/lager/measurement/dmm/dmm_net.py FUNCTIONS.
FUNCTIONS = (
    "dcv", "acv", "dci", "aci", "res", "fres", "cont", "diode",
    "freq", "period", "cap", "temp",
)

# Past the box's 60 s hardware_service budget for one reading (slow NPLC,
# auto-range, large capacitors), so the client never gives up first.
_HTTP_TIMEOUT_S = 75.0


def _parse_range(value: str | None) -> float | str | None:
    """``--range`` as a float, or "auto"."""
    if value is None or value.strip().lower() == "auto":
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise click.BadParameter(f"'{value}' is not a number or 'auto'",
                                 param_hint="--range") from exc


def _split_function_from_net(ctx, box_ip: str, netname: str | None,
                             function: str | None) -> tuple[str | None, str | None]:
    """Read ``lager dmm res`` as the default net and function ``res``.

    NET_NAME and FUNCTION are both optional positionals, so a lone function
    token lands in NET_NAME. It is taken as the function when a default DMM
    net is set and no DMM net has that name; a net named like a function
    still wins.
    """
    if function is not None or netname is None:
        return netname, function
    if netname.strip().lower() not in FUNCTIONS:
        return netname, function
    default = get_default_net(ctx, "dmm")
    if default is None:
        return netname, function
    token = netname.strip()
    nets = fetch_nets(box_ip)
    if any(n.get("name") == token and n.get("role") == DMM_ROLE for n in nets):
        return netname, function
    return default, token


@click.command(name="dmm", cls=NetCommand,
               help="Take one bench multimeter reading (default function: dcv)")
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--range", "range_", default=None, metavar="FULL_SCALE",
              help="Full-scale range in base units (20 = 20 V, 2e-6 = 2 uF), or 'auto' "
                   "(default)")
@click.option("--nplc", type=float, default=None,
              help="Integration time in power-line cycles (dcv, dci, res, fres only)")
@click.option("--sensor", default=None,
              help="Temperature probe for 'temp': rtd:pt100, rtd:pt1000, ther:k, ...")
@click.option("--errors", "show_errors", is_flag=True, default=False,
              help="Read and clear the meter's error queue instead of measuring")
@click.option("--idn", "show_idn", is_flag=True, default=False,
              help="Print the meter's *IDN? reply instead of measuring")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON object instead of formatted text")
@click.argument("netname", required=False, metavar="[NET_NAME]")
@click.argument("function", required=False, default=None,
                type=click.Choice(FUNCTIONS, case_sensitive=False))
def dmm(ctx, box, range_, nplc, sensor, show_errors, show_idn, as_json, netname, function):
    if show_errors and show_idn:
        raise click.UsageError("--errors and --idn cannot be combined")

    if netname is None:
        netname = get_default_net(ctx, "dmm")

    box_ip = resolve_box_locked(ctx, box, "dmm")
    netname, function = _split_function_from_net(ctx, box_ip, netname, function)

    if netname is None:
        display_nets(ctx, box_ip, None, DMM_ROLE, "dmm")
        return

    netname = netname.strip()
    if validate_net_exists(ctx, box_ip, netname, DMM_ROLE) is None:
        return  # Error already displayed

    if show_errors or show_idn:
        action = "errors" if show_errors else "identify"
        result = post_net_command(ctx, box_ip, netname, action, role=DMM_ROLE, quiet=True)
        if as_json:
            key = "errors" if show_errors else "idn"
            click.echo(json.dumps({"netname": netname, key: result.get("value")}))
        else:
            click.echo(result.get("message"))
        return

    function = (function or "dcv").lower()
    result = post_net_command(
        ctx, box_ip, netname, "measure", role=DMM_ROLE, quiet=True,
        http_timeout=_HTTP_TIMEOUT_S,
        function=function, range=_parse_range(range_), nplc=nplc, sensor=sensor,
    )
    overload = bool(result.get("overload"))
    if as_json:
        click.echo(json.dumps({
            "netname": netname,
            "function": function,
            "value": None if overload else result.get("value"),
            "unit": result.get("unit"),
            "overload": overload,
        }))
    elif overload:
        click.secho(result.get("message", "OVERLOAD"), fg="yellow")
    else:
        click.secho(result.get("message", ""), fg="green")


dmm.net_examples = [
    "lager dmm dmm1 --box <BOX>                      (DC volts, auto-range)",
    "lager dmm dmm1 acv --range 20 --box <BOX>",
    "lager dmm dmm1 res --nplc 100 --json --box <BOX>",
    "lager dmm dmm1 temp --sensor rtd:pt100 --box <BOX>",
    "lager dmm dmm1 --errors --box <BOX>",
    "lager dmm res --box <BOX>                       (default net, resistance)",
    "lager dmm --box <BOX>                           (list dmm nets)",
]
