# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
ADC (Analog-to-Digital Converter) command for reading analog voltages.

This module provides the `lager adc` command for reading voltage from ADC nets
on LabJack devices.
"""
from __future__ import annotations

import click

from ...context import get_default_net
from ...core.net_group import NetCommand
from ...core.net_helpers import (
    resolve_box,
    list_nets_by_role,
    display_nets_table,
    post_net_command,
    validate_net_exists,
)


ADC_ROLE = "adc"


@click.command(name="adc", cls=NetCommand, help="Read voltage from ADC net")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.argument("netname", required=False, metavar="[NET_NAME]")
def adc(ctx, box, netname):
    """Read voltage from an ADC (analog-to-digital converter) net.

    If no netname is provided, lists available ADC nets.
    """
    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'adc')

    box_ip = resolve_box(ctx, box)

    # If still no netname, list available ADC nets
    if netname is None:
        nets = list_nets_by_role(ctx, box_ip, ADC_ROLE)
        display_nets_table(nets, empty_message="No ADC nets found on this box.")
        return

    # Validate net exists before executing
    if validate_net_exists(ctx, box_ip, netname, ADC_ROLE) is None:
        return

    post_net_command(ctx, box_ip, netname, "read", role="adc")


adc.net_examples = [
    "lager adc adc1 --box <BOX>",
    "lager adc --box <BOX>          (list ADC nets)",
]
