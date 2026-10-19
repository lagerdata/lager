# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Argument parsing for net-style groups (``cli/core/net_group.NetGroup``).

Every NetGroup takes an optional net name before its subcommand. A stock Click
group fills that optional positional with the first token, so
``lager arm position --box X`` took ``position`` as the net name and failed
with "No such command '--box'". These tests parse with ``make_context``, which
runs no group callback, so nothing resolves a box or reads a default net.
"""
from types import SimpleNamespace

import click
import pytest

from cli.commands.communication.usb import usb
from cli.commands.development.arm import arm
from cli.commands.measurement.energy import energy
from cli.commands.measurement.logic import logic
from cli.commands.measurement.scope import scope
from cli.commands.measurement.watt import watt
from cli.commands.power.battery import battery
from cli.commands.power.eload import eload
from cli.commands.power.solar import solar
from cli.commands.power.supply import supply
from cli.core.net_group import NetGroup

GROUPS = [arm, battery, eload, energy, logic, scope, solar, supply, usb, watt]


def _parse(group, args):
    ctx = group.make_context(group.name, list(args), obj=SimpleNamespace())
    name = group._netname_param().name
    protected = getattr(ctx, "_protected_args", None)
    if protected is None:
        protected = ctx.protected_args
    command = protected[0] if protected else None
    return ctx.params[name], command, list(ctx.args), ctx.params.get("box")


def _a_subcommand(group):
    return sorted(group.commands)[0]


def test_every_group_is_a_net_group_with_one_optional_net_name():
    for group in GROUPS:
        assert isinstance(group, NetGroup), group.name
        assert group._netname_param() is not None, group.name


@pytest.mark.parametrize("group", GROUPS, ids=lambda g: g.name)
class TestNetNameIsOptional:
    def test_subcommand_first_leaves_the_net_name_unset(self, group):
        sub = _a_subcommand(group)
        net, command, rest, _ = _parse(group, [sub, "--box", "DEMO"])
        assert net is None
        assert command == sub
        assert rest == ["--box", "DEMO"]

    def test_bare_subcommand_leaves_the_net_name_unset(self, group):
        sub = _a_subcommand(group)
        net, command, rest, _ = _parse(group, [sub])
        assert (net, command, rest) == (None, sub, [])

    def test_net_name_before_the_subcommand_is_kept(self, group):
        sub = _a_subcommand(group)
        net, command, rest, _ = _parse(group, ["net1", sub, "--box", "DEMO"])
        assert (net, command, rest) == ("net1", sub, ["--box", "DEMO"])

    def test_net_named_like_a_subcommand_is_kept_when_a_subcommand_follows(self, group):
        sub = _a_subcommand(group)
        net, command, _, _ = _parse(group, [sub, sub, "--box", "DEMO"])
        assert (net, command) == (sub, sub)

    def test_group_level_box_before_the_subcommand(self, group):
        # This form used to take the subcommand as the net name and list nets.
        sub = _a_subcommand(group)
        net, command, _, box = _parse(group, ["--box", "DEMO", sub])
        assert (net, command, box) == (None, sub, "DEMO")

    def test_group_level_box_then_net_then_subcommand(self, group):
        sub = _a_subcommand(group)
        net, command, _, box = _parse(group, ["--box", "DEMO", "net1", sub])
        assert (net, command, box) == ("net1", sub, "DEMO")

    def test_no_subcommand_is_the_listing_form(self, group):
        net, command, _, box = _parse(group, ["--box", "DEMO"])
        assert (net, command, box) == (None, None, "DEMO")

    def test_unknown_first_token_is_still_a_net_name(self, group):
        net, _, _, _ = _parse(group, ["not-a-command", "--box", "DEMO"])
        assert net == "not-a-command"


def test_a_group_without_a_net_name_parses_as_stock_click():
    @click.group(cls=NetGroup)
    def grp():
        pass

    @grp.command()
    def status():
        pass

    assert grp._netname_param() is None
    ctx = grp.make_context("grp", ["status"])
    protected = getattr(ctx, "_protected_args", None) or ctx.protected_args
    assert protected == ["status"]
