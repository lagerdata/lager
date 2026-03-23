# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
GPI (GPIO Input) command for reading digital input states.

This module provides the `lager gpi` command for reading GPIO input state
(0 or 1) from LabJack devices.
"""
from __future__ import annotations

import json

import click

from ...context import get_default_net
from ...core.net_helpers import (
    resolve_box,
    list_nets_by_role,
    display_nets_table,
    run_impl_script,
    validate_net_exists,
)


GPIO_ROLE = "gpio"


@click.command(name="gpi", help="Read GPIO input state (0 or 1)")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option(
    "--wait-for",
    "wait_for",
    type=click.Choice(["high", "low", "1", "0"], case_sensitive=False),
    default=None,
    help="Block until pin reaches this level",
)
@click.option("--timeout", type=float, default=None, help="Timeout in seconds for --wait-for")
@click.option("--scan-rate", type=int, default=None, help="LabJack streaming sample rate in Hz (advanced)")
@click.option("--scans-per-read", type=int, default=None, help="LabJack scans per read batch (advanced)")
@click.option("--poll-interval", type=float, default=None, help="Poll interval in seconds for non-streaming drivers (advanced)")
@click.argument("netname", required=False)
def gpi(ctx, box, wait_for, timeout, scan_rate, scans_per_read, poll_interval, netname):
    """Read the state of a GPIO input net.

    Returns 0 (low) or 1 (high).
    If no netname is provided, lists available GPIO nets.

    Use --wait-for to block until the pin reaches a target level.
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

    if wait_for is not None:
        payload_dict = {
            "netname": netname,
            "action": "wait_for_level",
            "level": wait_for,
        }
        if timeout is not None:
            payload_dict["timeout"] = timeout
        if scan_rate is not None:
            payload_dict["scan_rate"] = scan_rate
        if scans_per_read is not None:
            payload_dict["scans_per_read"] = scans_per_read
        if poll_interval is not None:
            payload_dict["poll_interval"] = poll_interval
        payload = json.dumps(payload_dict)
    else:
        payload = json.dumps({"netname": netname, "action": "input"})

    run_impl_script(
        ctx=ctx,
        box=box_ip,
        impl_script="gpio.py",
        args=(payload,),
        timeout=None,
    )
