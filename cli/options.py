# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Shared Click option decorators for CLI commands.
"""

import click


def _force_command_callback(ctx, param, value):
    """Update ctx.obj.force_command when the local flag is used."""
    if value and hasattr(ctx, 'obj') and ctx.obj:
        ctx.obj.force_command = True
    return value


force_command_option = click.option(
    '--force-command',
    is_flag=True,
    default=False,
    expose_value=False,
    callback=_force_command_callback,
    is_eager=True,
    help='Bypass command-in-progress lock',
)
