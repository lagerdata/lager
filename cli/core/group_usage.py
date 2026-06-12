# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Usage-line formatting shared by lager's plain command groups.

Click's default group usage reads ``lager nets [OPTIONS] COMMAND
[ARGS]...`` even when no subcommand takes positional arguments, which
reads as noise to users. Net-style groups already rewrite their usage
via NetGroupHelpMixin (cli/core/net_group.py); this module covers the
remaining plain groups.
"""
from __future__ import annotations

import click


class CommandFirstUsageMixin:
    """Usage reads ``lager nets COMMAND [OPTIONS]`` instead of
    ``lager nets [OPTIONS] COMMAND [ARGS]...``.

    Group-level options are dropped from the usage line (they still
    appear in the Options section) — the trailing [OPTIONS] is what
    users actually type after a subcommand. Argument metavars are
    preserved via ``param.get_usage_pieces()``. Groups invokable
    without a subcommand show ``[COMMAND]``.
    """

    def collect_usage_pieces(self, ctx: click.Context) -> list:
        pieces = []
        for param in self.get_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))  # arguments only; options yield []
        cmd = "[COMMAND]" if self.invoke_without_command else "COMMAND"
        return [*pieces, cmd, "[OPTIONS]"]


class LagerGroup(CommandFirstUsageMixin, click.Group):
    """Default group class for lager's plain (non-net) command groups."""
