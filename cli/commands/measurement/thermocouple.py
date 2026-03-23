# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Thermocouple commands for temperature measurement.
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

THERMOCOUPLE_ROLE = "thermocouple"


@click.command(name="thermocouple", help="Read thermocouple temperature in degrees Celsius")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.argument("netname", required=False)
def thermocouple(ctx, box, netname):
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

    payload = json.dumps({"netname": netname})

    try:
        run_python_internal(
            ctx=ctx,
            runnable=get_impl_path("thermocouple.py"),
            box=box_ip,
            env=(),
            passenv=(),
            kill=False,
            download=(),
            allow_overwrite=False,
            signum="SIGTERM",
            timeout=30,  # Add reasonable timeout for temperature reading
            detach=False,
            port=(),
            org=None,
            args=[payload],
        )
    except SystemExit as e:
        # Re-raise non-zero exits (error already displayed by run_python_internal)
        if e.code != 0:
            raise
    except Exception as e:
        click.secho(f"Error reading thermocouple: {e}", fg='red', err=True)
        click.secho("Troubleshooting tips:", err=True)
        click.secho(f"  1. Check thermocouple is connected to Phidget device", err=True)
        click.secho(f"  2. Verify net configuration: lager nets --box {box or box_ip}", err=True)
        click.secho(f"  3. Check box connectivity: lager hello --box {box or box_ip}", err=True)
        ctx.exit(1)
