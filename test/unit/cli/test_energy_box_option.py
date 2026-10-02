#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""`lager energy --box <BOX>` reaches the named box (cli/commands/measurement/energy.py).

The group's own help lists `lager energy --box <BOX>` as the way to list
energy-analyzer nets, and every other role group accepts `--box` there. The
group used to declare only NET_NAME, so the listing failed with
`No such option '--box'` and could reach only the default box.

As in `lager watt`, a group-level `--box` also serves a subcommand that was
given none, and a subcommand's own `--box` wins.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

energy_mod = importlib.import_module("cli.commands.measurement.energy")
energy_group = energy_mod.energy

_STAT = {"mean": 1.0, "min": 0.5, "max": 1.5, "std": 0.1}
_VALUES = {
    "read_energy": {"duration_s": 1.0, "energy_j": 1.0, "energy_wh": 0.0003,
                    "charge_c": 0.3, "charge_ah": 0.0001},
    "read_stats": {"duration_s": 1.0, "current": _STAT, "voltage": _STAT, "power": _STAT},
}


class _Obj:
    """Settable stand-in for the LagerContext (the group stashes attrs on it)."""


def _run(args):
    """Invoke the energy group with the box boundary mocked.

    Returns (result, boxes) where boxes records the `box` argument each
    resolver received, tagged with the resolver's name.
    """
    boxes: list[tuple[str, object]] = []

    def fake_resolve(ctx, box, read_only=False):
        boxes.append(("resolve_box", box))
        return "1.2.3.4"

    def fake_resolve_locked(ctx, box, cmd):
        boxes.append(("resolve_box_locked", box))
        return "1.2.3.4"

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False,
                  http_timeout=None, **params):
        return {"success": True, "value": _VALUES[action], "message": "ok"}

    with patch.object(energy_mod, "post_net_command", fake_post), \
         patch.object(energy_mod, "resolve_box", fake_resolve), \
         patch.object(energy_mod, "resolve_box_locked", fake_resolve_locked), \
         patch.object(energy_mod, "validate_net_exists",
                      lambda ctx, ip, name, role: {"name": name}), \
         patch.object(energy_mod, "display_nets", MagicMock()), \
         patch.object(energy_mod, "get_default_net", lambda ctx, t: None):
        result = CliRunner().invoke(
            energy_group, args, obj=_Obj(), catch_exceptions=False
        )
    return result, boxes


def test_group_box_lists_nets_on_that_box():
    result, boxes = _run(["--box", "BENCH"])
    assert result.exit_code == 0, result.output
    assert boxes == [("resolve_box", "BENCH")]


def test_no_box_still_lists_the_default_box():
    result, boxes = _run([])
    assert result.exit_code == 0, result.output
    assert boxes == [("resolve_box", None)]


@pytest.mark.parametrize("subcmd", ["read", "stats"])
def test_group_box_serves_a_subcommand_without_one(subcmd):
    result, boxes = _run(["--box", "BENCH", "e1", subcmd, "--duration", "1"])
    assert result.exit_code == 0, result.output
    assert boxes == [("resolve_box_locked", "BENCH")]


@pytest.mark.parametrize("subcmd", ["read", "stats"])
def test_a_subcommand_box_wins(subcmd):
    result, boxes = _run(["--box", "GROUP", "e1", subcmd, "--duration", "1",
                          "--box", "LEAF"])
    assert result.exit_code == 0, result.output
    assert boxes == [("resolve_box_locked", "LEAF")]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
