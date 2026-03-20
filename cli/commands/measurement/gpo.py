# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
GPO (GPIO Output) command for setting digital output states.

This module provides the `lager gpo` command for setting GPIO output level
on LabJack devices.
"""
from __future__ import annotations

import json

import click

from ...context import get_default_net
from ...options import force_command_option
from ...core.net_helpers import (
    resolve_box,
    list_nets_by_role,
    display_nets_table,
    run_impl_script,
    validate_net_exists,
)


GPIO_ROLE = "gpio"


@click.command(name="gpo", help="Set GPIO output level (0/1, low/high, off/on, toggle)")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--hold", is_flag=True, default=False,
              help="Hold output state (keeps process alive until Ctrl+C)")
@click.argument("netname", required=False)
@click.argument("level", required=False,
                type=click.Choice(["low", "high", "on", "off", "0", "1", "toggle"], case_sensitive=False))
@force_command_option
def gpo(ctx, box, netname, level, hold):
    """Set the output level of a GPIO output net.

    Level can be: low, high, on, off, 0, 1, or toggle.
    If no netname is provided, lists available GPIO nets.
    """
    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'gpio')

    box_ip = resolve_box(ctx, box)

    # If still no netname, list available GPIO nets
    if netname is None:
        nets = list_nets_by_role(ctx, box_ip, GPIO_ROLE)
        display_nets_table(nets, empty_message="No GPIO nets found on this box.")
        return

    # Validate net exists with GPIO role
    net = validate_net_exists(ctx, box_ip, netname, GPIO_ROLE)
    if net is None:
        return  # Error already displayed

    # If we have a net but no level, show error with detailed explanation
    if level is None:
        click.secho("Error: LEVEL argument required", fg='red', err=True)
        click.echo("\nUsage: lager gpo <NET_NAME> <LEVEL>", err=True)
        click.echo("\nAvailable levels:", err=True)
        click.echo("  high, on, 1   - Set output HIGH (typically 3.3V or 5V)", err=True)
        click.echo("  low, off, 0   - Set output LOW (0V / ground)", err=True)
        click.echo("  toggle        - Invert current state (HIGH->LOW or LOW->HIGH)", err=True)
        click.echo(f"\nExample: lager gpo {netname} high --box {box or '<box>'}", err=True)
        ctx.exit(1)

    payload = json.dumps({"netname": netname, "action": "output", "level": level, "hold": hold})

    run_impl_script(
        ctx=ctx,
        box=box_ip,
        impl_script="gpio.py",
        args=(payload,),
        timeout=None,
    )
