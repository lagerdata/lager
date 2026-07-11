# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Thermocouple commands for temperature measurement.
"""
from __future__ import annotations

import json

import click
from ...context import get_default_net
from ...core.net_group import NetCommand
from ...core.net_helpers import (
    resolve_box,
    display_nets,
    post_net_command,
    validate_net_exists,
)

THERMOCOUPLE_ROLE = "thermocouple"


@click.command(name="thermocouple", cls=NetCommand, help="Read thermocouple temperature in degrees Celsius")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON object instead of formatted text")
@click.argument("netname", required=False, metavar="[NET_NAME]")
def thermocouple(ctx, box, netname, as_json):
    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'thermocouple')

    box_ip = resolve_box(ctx, box)

    # If still no netname, list available thermocouple nets
    if netname is None:
        display_nets(ctx, box_ip, None, THERMOCOUPLE_ROLE, "thermocouple")
        return

    # Strip whitespace from netname for better UX
    netname = netname.strip()

    # Validate net exists with correct role
    net = validate_net_exists(ctx, box_ip, netname, THERMOCOUPLE_ROLE)
    if net is None:
        return  # Error already displayed by validate_net_exists

    result = post_net_command(ctx, box_ip, netname, "read", role="thermocouple",
                              quiet=True)
    temperature = float(result.get("value"))

    if as_json:
        click.echo(json.dumps({
            "netname": netname, "temperature_c": temperature,
        }))
    else:
        click.secho(f"Temperature: {temperature}˚C", fg="green")


thermocouple.net_examples = [
    "lager thermocouple tc1 --box <BOX>",
    "lager thermocouple tc1 --json --box <BOX>",
    "lager thermocouple --box <BOX>     (list thermocouple nets)",
]
