# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
A role that is one net with no channel, spelled as an empty channel list.

A scope net stands for the whole instrument rather than any of its inputs,
so it has no channel. Every other role has one per net, and the tables give
each role a list of them -- so "none" had to be said somehow, and an empty
list says it without inventing a sentinel that three call sites would have
to agree on.

The reason this is worth its own file is what happens if a channel leaks
onto a scope net. The box tells a scope from one of its channels by whether
the record carries a pin: that is what lets an old saved net, from before
the roles split, be recognised as the channel it really is. A scope net
saved with pin 1 is indistinguishable from channel A, and the migration
converts it back into one on the next read -- quietly, and to a net the user
named.
"""
from __future__ import annotations

import importlib

import pytest

# `from cli.commands.box import nets` binds the click group, not the module
# it lives in, so the tables and helpers below would be invisible.
nets_mod = importlib.import_module("cli.commands.box.nets")


class TestAnEmptyListMeansOneNetWithNoChannel:

    def test_it_expands_to_a_single_entry(self):
        assert nets_mod.expand_channels([]) == [nets_mod.CHANNEL_LESS]

    def test_that_entry_carries_no_channel(self):
        """Not "1", which would look like the first channel."""
        assert nets_mod.expand_channels([])[0] == ""

    def test_a_real_list_is_left_alone(self):
        assert nets_mod.expand_channels(["1", "2"]) == ["1", "2"]

    def test_none_is_treated_as_empty(self):
        """A role absent from the table reaches here as None."""
        assert nets_mod.expand_channels(None) == [nets_mod.CHANNEL_LESS]

    def test_the_marker_reads_as_no_pin_to_the_box(self):
        """The two halves have to agree or the migration eats the net."""
        from lager.nets.scope_migration import pin_of

        assert pin_of({"pin": nets_mod.CHANNEL_LESS}) is None


class TestTheScopeRoleIsChannelLess:

    def test_the_tables_say_so(self):
        import ast
        import pathlib

        scanner = pathlib.Path(__file__).resolve().parents[3] / "box" / "lager" \
            / "http_handlers" / "usb_scanner.py"
        tree = ast.parse(scanner.read_text())

        def named(node):
            # CHANNEL_MAPS carries a type annotation, so it parses as an
            # AnnAssign with a single target rather than an Assign.
            if isinstance(node, ast.AnnAssign):
                return [node.target]
            return node.targets if isinstance(node, ast.Assign) else []

        maps = next(
            ast.literal_eval(node.value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(getattr(t, "id", None) == "CHANNEL_MAPS" for t in named(node)))

        assert maps["Picoscope_2000"]["scope"] == []
        assert maps["Picoscope_2000"]["scope-channel"] == ["1", "2"]

    def test_a_scope_offers_both_roles(self):
        for instrument in ("Picoscope_2000", "Rigol_MSO5204"):
            roles = nets_mod.INSTRUMENT_NET_MAP[instrument]
            assert "scope" in roles and "scope-channel" in roles

    def test_the_rigol_keeps_its_logic_role(self):
        assert "logic" in nets_mod.INSTRUMENT_NET_MAP["Rigol_MSO5204"]


class TestChannelLessNetsAreOfferedNotHidden:
    """An empty list must not expand to nothing, or the net disappears."""

    def test_a_two_channel_scope_offers_three_nets(self):
        channels = {"scope": [], "scope-channel": ["1", "2"]}
        offered = sum(len(nets_mod.expand_channels(chs))
                      for chs in channels.values())
        assert offered == 3

    def test_a_four_channel_scope_offers_five(self):
        channels = {"scope": [], "scope-channel": ["1", "2", "3", "4"]}
        offered = sum(len(nets_mod.expand_channels(chs))
                      for chs in channels.values())
        assert offered == 5
