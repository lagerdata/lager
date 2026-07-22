# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Deprecated `lager box` group. The subcommands moved to top level —
`lager box config` became `lager box-config`, and `lager box dut`
became `lager dut`. This hidden group keeps the old spellings working
with a deprecation warning until they are removed.
"""
import click

from .config import box_config
from .dut import box_dut
from ...core.group_usage import LagerGroup

_RENAMES = {
    "config": "lager box-config",
    "dut": "lager dut",
}


@click.group(name="box", cls=LagerGroup, hidden=True)
@click.pass_context
def box(ctx: click.Context) -> None:
    """Deprecated aliases for `lager box-config` and `lager dut`."""
    new = _RENAMES.get(ctx.invoked_subcommand)
    if new:
        click.secho(
            f"DEPRECATED: `lager box {ctx.invoked_subcommand}` is now `{new}`. "
            "The old spelling still works but will be removed in a future release.",
            fg="yellow", err=True,
        )


box.add_command(box_config, name="config")
box.add_command(box_dut, name="dut")
