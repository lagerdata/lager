# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
`lager box` — top-level group for box-scoped commands: declarative
configuration (`config`) and DUT context authoring (`dut`).
"""
import click

from .config import box_config
from .dut import box_dut
from ...core.group_usage import LagerGroup


@click.group(name="box", cls=LagerGroup)
def box() -> None:
    """Box-side configuration and provisioning"""


box.add_command(box_config, name="config")
box.add_command(box_dut, name="dut")
