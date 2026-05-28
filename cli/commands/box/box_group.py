# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
`lager box` — top-level group for box-side declarative configuration.

PR #1 only registers the `config` sub-tree. Reserved for future
box-scoped commands (status, exec, etc.) so we don't have to migrate
later.
"""
import click

from .config import box_config


@click.group(name="box")
def box() -> None:
    """Box-side configuration and provisioning"""


box.add_command(box_config, name="config")
