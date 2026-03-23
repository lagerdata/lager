# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
DAC (Digital-to-Analog Converter) command for setting analog voltages.

This module provides the `lager dac` command for setting or reading voltage
on DAC nets connected to LabJack devices.
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


DAC_ROLE = "dac"


@click.command(name="dac", help="Set or read DAC output voltage")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.argument("netname", required=False)
@click.argument("voltage", required=False)
def dac(ctx, box, netname, voltage):
    """Set or read voltage from a DAC (digital-to-analog converter) net.

    If no voltage is provided, reads the current DAC value.
    If no netname is provided, lists available DAC nets.
    """
    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'dac')

    box_ip = resolve_box(ctx, box)

    # If still no netname, list available DAC nets
    if netname is None:
        nets = list_nets_by_role(ctx, box_ip, DAC_ROLE)
        display_nets_table(nets, empty_message="No DAC nets found on this box.")
        return

    # Validate net exists before executing
    if validate_net_exists(ctx, box_ip, netname, DAC_ROLE) is None:
        return

    # Validate voltage if provided
    if voltage is not None:
        if voltage.strip() == "":
            click.secho("Error: Voltage argument cannot be empty", fg='red', err=True)
            click.secho("Usage: lager dac <netname> [voltage]", err=True)
            ctx.exit(1)

        # Try to parse as float
        try:
            voltage_float = float(voltage)
        except ValueError:
            click.secho(f"Error: '{voltage}' is not a valid voltage value", fg='red', err=True)
            click.secho("Voltage must be a number (e.g., 3.3, 5.0)", err=True)
            ctx.exit(1)

        # Validate voltage range (LabJack DAC range is 0-5V or 0-10V depending on config)
        DAC_MIN_VOLTAGE, DAC_MAX_VOLTAGE = 0.0, 10.0
        if voltage_float < DAC_MIN_VOLTAGE or voltage_float > DAC_MAX_VOLTAGE:
            click.secho(f"Error: Voltage must be between {DAC_MIN_VOLTAGE} and {DAC_MAX_VOLTAGE} V, got {voltage_float} V", fg='red', err=True)
            ctx.exit(1)

    payload = {"netname": netname}
    if voltage is not None:
        payload["voltage"] = voltage
    payload_json = json.dumps(payload)

    run_impl_script(
        ctx=ctx,
        box=box_ip,
        impl_script="dac.py",
        args=(payload_json,),
        timeout=None,
    )
