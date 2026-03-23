# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Watt meter commands for power measurement.
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

WATT_ROLE = "watt-meter"

# Timeout for watt meter readings (seconds)
WATT_TIMEOUT = 30


@click.command(name="watt", help="Read power from watt meter net (returns watts)")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.argument("netname", required=False)
def watt(ctx, box, netname):
    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'watt')

    box_ip = resolve_box(ctx, box)

    # If still no netname, list available watt meter nets
    if netname is None:
        display_nets(ctx, box_ip, None, WATT_ROLE, "watt meter")
        return

    # Strip whitespace from netname for better UX
    netname = netname.strip()

    # Validate net exists before executing command
    net = validate_net_exists(ctx, box_ip, netname, WATT_ROLE)
    if net is None:
        return  # Error already displayed

    payload = json.dumps({"netname": netname})

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
            timeout=WATT_TIMEOUT,
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
        click.secho(f"Error: Failed to read watt meter", fg='red', err=True)
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
