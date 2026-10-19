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
  * ``--box``      names the target Lager Box and goes **after** the command;
    ``BOX_NAME`` falls back to the configured default box.

Click's stock usage line for a group is ``[OPTIONS] COMMAND [ARGS]...``. That
is actively misleading here: it implies ``--box`` is a *group* option that
precedes the command (``lager debug --box <BOX> reset``), when in fact
``--box`` is defined on each subcommand and must follow it
(``lager debug reset --box <BOX>``). New users hit this constantly.

Leaf commands get the same treatment: Click's stock leaf usage puts the
options first (``lager uart [OPTIONS] [NET_NAME] ...``), which reads backwards
next to the group usage and next to every example we print. All net-style leaf
usage lines are therefore rewritten to *positionals first*, then an explicit
``--box [BOX_NAME]`` tail whenever the command accepts ``--box``. As on the
group usage lines, ``[OPTIONS]`` is left out — the Options section right below
lists them::

    lager uart [NET_NAME] --box [BOX_NAME]
    lager supply [NET_NAME] voltage [VALUE] --box [BOX_NAME]

This module supplies these building blocks:

  * :class:`NetExamplesMixin` — appends a verbatim ``Examples`` section from a
    ``net_examples`` attribute. Works on any ``click.Command`` (group or leaf).
  * :class:`NetGroup` — a ``click.Group`` whose usage line is rewritten to the
    real ``[NET_NAME] [COMMAND] --box [BOX_NAME]`` pattern, plus examples.
    Subcommands created via ``@group.command()`` default to
    :class:`NetSubCommand` so their usage lines match without opting in.
  * :class:`NetSubCommand` — a leaf command under a net group. Renders
    positionals-first usage, and re-inserts ``[NET_NAME]`` for groups that
    extract it positionally without declaring a Click argument (the ``debug``
    group).
  * :class:`NetCommand` — a standalone leaf net command (``adc``, ``uart`` ...)
    with positionals-first usage and an ``Examples`` section.

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

#: Usage tail advertising the box target, spelled identically everywhere.
BOX_USAGE_TAIL = "--box [BOX_NAME]"


def _leaf_usage_pieces(cmd: click.Command, ctx: click.Context) -> list[str]:
    """Usage pieces for a net-style leaf command.

    Click's default is ``[OPTIONS] <positionals>``; net commands are
    documented and used as ``<positionals> --box [BOX_NAME]`` (options may
    legally appear anywhere, so this order is valid — it is just the one
    every example uses). Like the net-group usage lines, ``[OPTIONS]`` is
    omitted — the Options section below lists them — except when the command
    has no ``--box``, where it remains so the usage line isn't bare.
    """
    pieces = [p for p in cmd.collect_usage_pieces(ctx) if p != cmd.options_metavar]
    if any(param.name == "box" for param in cmd.params):
        pieces.append(BOX_USAGE_TAIL)
    else:
        pieces.append(cmd.options_metavar)
    return pieces


class HiddenArgument(click.Argument):
    """A positional argument omitted from the usage line.

    For rarely-used trailing positionals (e.g. ``lager uart``'s
    ``serial-port`` action) that would clutter the primary usage; document
    them in the command's help body instead. Parsing and validation are
    unaffected.
    """

    def get_usage_pieces(self, ctx: click.Context) -> list[str]:
        return []


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

    #: Subcommands declared via ``@group.command()`` default to
    #: :class:`NetSubCommand` so their usage lines are positionals-first and
    #: end in ``--box [BOX_NAME]`` without opting in individually. (Assigned
    #: after the class definition below — NetSubCommand isn't defined yet.)
    command_class: type | None = None

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if getattr(self, "net_takes_netname", True):
            args = f"{NET_NAME_METAVAR} [COMMAND] {BOX_USAGE_TAIL}"
        else:
            args = f"[COMMAND] {BOX_USAGE_TAIL}"
        formatter.write_usage(ctx.command_path, args)


class NetSubCommand(NetExamplesMixin, click.Command):
    """A leaf command under a net-style group.

    Renders positionals-first usage (see :func:`_leaf_usage_pieces`). For
    groups that extract ``NET_NAME`` positionally *without* declaring it as a
    Click argument (the ``debug`` group), Click can't see that positional and
    the stock usage would omit it — so it is re-inserted between the group and
    the subcommand: ``lager debug [NET_NAME] reset [OPTIONS] --box [BOX_NAME]``.
    Groups that declare ``NETNAME`` as a real argument (``supply`` etc.)
    already get it via ``ctx.command_path``.
    """

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        prog = ctx.command_path
        parent = ctx.parent
        if parent is not None:
            parent_cmd = parent.command
            declares_args = any(
                isinstance(param, click.Argument) for param in parent_cmd.params
            )
            if getattr(parent_cmd, "net_takes_netname", False) and not declares_args:
                prog = " ".join(
                    p for p in (parent.command_path, NET_NAME_METAVAR, self.name) if p
                )
        formatter.write_usage(prog, " ".join(_leaf_usage_pieces(self, ctx)))


NetGroupHelpMixin.command_class = NetSubCommand


#: Fills the optional net-name argument when the command line starts with a
#: subcommand, and is swapped back to None before the group callback runs.
_NO_NET_NAME = "\0no-net-name"


class NetGroup(NetGroupHelpMixin, click.Group):
    """A ``click.Group`` for net-style commands. See the module docstring.

    The net name is optional, but a stock Click group fills an optional
    positional with the first token it sees. ``lager arm position --box X``
    therefore took ``position`` as the net name and then failed with
    "No such command '--box'", and ``lager arm --box X position`` took it as
    the net name and listed nets instead of reading the position. When the
    first positional token is a subcommand and the token after it is not, the
    net name is left unset and the group falls back to the default net. A net
    that shares a subcommand's name still works when a subcommand follows it
    (``lager arm position position``).
    """

    def _netname_param(self):
        args = [p for p in self.params if isinstance(p, click.Argument)]
        if len(args) == 1 and not args[0].required and args[0].nargs == 1:
            return args[0]
        return None

    def _first_positional(self, args: list[str]):
        """Index of the first token that is not one of this group's options."""
        takes_value = {
            opt
            for p in self.params
            if isinstance(p, click.Option) and not p.is_flag and not p.count
            for opt in p.opts
        }
        i = 0
        while i < len(args):
            token = args[i]
            if token == "--" or not token.startswith("-") or token == "-":
                return i if token != "--" else None
            if token in takes_value:
                i += 1  # the option's value
            i += 1
        return None

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        param = self._netname_param()
        name = param.name if param is not None else None
        i = self._first_positional(args) if name is not None else None
        starts_with_subcommand = (
            i is not None
            and args[i] in self.commands
            and (i + 1 >= len(args) or args[i + 1] not in self.commands)
        )
        if name is None or i is None or not starts_with_subcommand:
            return super().parse_args(ctx, args)
        rest = super().parse_args(ctx, [*args[:i], _NO_NET_NAME, *args[i:]])
        if ctx.params.get(name) == _NO_NET_NAME:
            ctx.params[name] = None
        return rest


class NetCommand(NetExamplesMixin, click.Command):
    """A standalone leaf net command (e.g. ``adc``, ``uart``) with
    positionals-first usage and an ``Examples`` section."""

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_usage(ctx.command_path, " ".join(_leaf_usage_pieces(self, ctx)))


class BoxCommand(click.Command):
    """A leaf command whose target is a box, not a net.

    Renders its usage as ``... --box [BOX_NAME]`` so box-scoped commands
    (e.g. ``lager ssh-setup``) read consistently with the net-style
    commands instead of the bare ``[OPTIONS]``. ``--box`` is still listed
    under Options.
    """

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_usage(ctx.command_path, BOX_USAGE_TAIL)
