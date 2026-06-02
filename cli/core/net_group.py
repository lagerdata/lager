# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Shared help formatting for net-style CLI commands.

Net commands all follow the same shape::

    lager [TYPE] [NET_NAME] [COMMAND] --box [BOX_NAME]

where:
  * ``[TYPE]``     is the group itself (``debug``, ``supply``, ...).
  * ``[NET_NAME]`` is optional and falls back to the configured default net.
  * ``[COMMAND]``  is the subcommand (``reset``, ``voltage``, ...).
  * ``--box``      names the target Lagerbox and goes **after** the command;
    ``BOX_NAME`` falls back to the configured default box.

Click's stock usage line for a group is ``[OPTIONS] COMMAND [ARGS]...``. That
is actively misleading here: it implies ``--box`` is a *group* option that
precedes the command (``lager debug --box <BOX> reset``), when in fact
``--box`` is defined on each subcommand and must follow it
(``lager debug reset --box <BOX>``). New users hit this constantly.

This module supplies three building blocks:

  * :class:`NetExamplesMixin` — appends a verbatim ``Examples`` section from a
    ``net_examples`` attribute. Works on any ``click.Command`` (group or leaf).
  * :class:`NetGroup` — a ``click.Group`` whose usage line is rewritten to the
    real ``[NET_NAME] [COMMAND] --box [BOX_NAME]`` pattern, plus examples.
  * :class:`NetSubCommand` — a leaf command under a group that extracts
    ``NET_NAME`` positionally without declaring it as a Click argument (the
    ``debug`` group). Re-inserts ``[NET_NAME]`` into the subcommand usage line.
  * :class:`NetCommand` — a standalone leaf net command (``adc``, ``uart`` ...)
    that just needs an ``Examples`` section.

Set ``net_examples`` on the resulting command object; the metavar for the
positional net name is exported as :data:`NET_NAME_METAVAR` so it stays
spelled identically everywhere.
"""
from __future__ import annotations

from typing import Sequence

import click

#: Metavar used for the optional positional net name, so the group usage line
#: and every subcommand usage line spell it identically (``[NET_NAME]``).
NET_NAME_METAVAR = "[NET_NAME]"


class NetExamplesMixin:
    """Append a verbatim ``Examples`` section built from ``net_examples``.

    Mix into any ``click.Command``/``click.Group`` subclass and set
    ``net_examples`` (a sequence of example invocation strings). Each entry is
    printed verbatim (no re-wrapping / whitespace collapsing) so the commands
    are copy-paste accurate.
    """

    net_examples: Sequence[str] = ()

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        # Preserve any epilog the underlying Command class would render.
        super().format_epilog(ctx, formatter)

        examples = getattr(self, "net_examples", ()) or ()
        if not examples:
            return

        with formatter.section("Examples"):
            indent = " " * formatter.current_indent
            for line in examples:
                formatter.write(f"{indent}{line}\n")


class NetGroupHelpMixin(NetExamplesMixin):
    """Mixin that renders the net-style usage line + an ``Examples`` section.

    ``net_takes_netname`` controls whether ``[NET_NAME]`` appears in the usage
    line (true for every current net group).
    """

    #: Whether the group accepts an optional NET_NAME before the command.
    net_takes_netname: bool = True

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if getattr(self, "net_takes_netname", True):
            args = f"{NET_NAME_METAVAR} [COMMAND] --box [BOX_NAME]"
        else:
            args = "[COMMAND] --box [BOX_NAME]"
        formatter.write_usage(ctx.command_path, args)


class NetGroup(NetGroupHelpMixin, click.Group):
    """A ``click.Group`` for net-style commands. See the module docstring."""


class NetSubCommand(NetExamplesMixin, click.Command):
    """A leaf command whose parent group extracts ``NET_NAME`` positionally
    *without* declaring it as a Click argument (the ``debug`` group).

    Click can't see that positional, so the stock subcommand usage omits it
    (``lager debug reset [OPTIONS]``). This re-inserts it so the usage matches
    every other net group: ``lager debug [NET_NAME] reset [OPTIONS]``.
    """

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        parent_path = ctx.parent.command_path if ctx.parent else ""
        prog = " ".join(p for p in (parent_path, NET_NAME_METAVAR, self.name) if p)
        formatter.write_usage(prog, " ".join(self.collect_usage_pieces(ctx)))


class NetCommand(NetExamplesMixin, click.Command):
    """A standalone leaf net command (e.g. ``adc``) with an ``Examples`` section."""
