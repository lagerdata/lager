#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the `lager watt` net group (cli/commands/measurement/watt.py).

The watt command is a NetGroup that reads power/current/voltage/all. It now
drives the box over the warm HTTP API (POST :9000/net/command via
``post_net_command``) instead of the legacy :5000 ``lager python`` path. These
tests assert the CLI sends the right action + duration, formats the returned
value (including ``--json``), and preserves the original default behaviors
(``lager watt NET`` reads power; ``lager watt`` lists nets).

The box is mocked at the ``post_net_command`` boundary, so no hardware or
network is touched.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

watt_mod = importlib.import_module("cli.commands.measurement.watt")
watt_group = watt_mod.watt


class _Obj:
    """Settable stand-in for the LagerContext (the group stashes attrs on it)."""


# Canned box responses keyed by action, so the CLI has a value to format.
_VALUES = {
    "power": 0.5,
    "current": 0.0123,
    "voltage": 3.3,
    "all": {"current": 0.1, "voltage": 3.3, "power": 0.33},
}


def _run(args):
    """Invoke the watt group with the box boundary mocked.

    Returns (result, calls, display_mock) where each call is a dict of the
    kwargs post_net_command received (netname/action/params...).
    """
    calls: list[dict] = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False, **params):
        calls.append({"box_ip": box_ip, "netname": netname, "action": action,
                      "role": role, "quiet": quiet, "params": params})
        return {"success": True, "value": _VALUES[action], "message": "ok"}

    display_mock = MagicMock()

    with patch.object(watt_mod, "post_net_command", fake_post), \
         patch.object(watt_mod, "resolve_box", lambda ctx, box: "1.2.3.4"), \
         patch.object(watt_mod, "validate_net_exists",
                      lambda ctx, ip, name, role: {"name": name}), \
         patch.object(watt_mod, "display_nets", display_mock), \
         patch.object(watt_mod, "get_default_net", lambda ctx, t: None):
        result = CliRunner().invoke(
            watt_group, args, obj=_Obj(), catch_exceptions=False
        )
    return result, calls, display_mock


# --------------------------------------------------------------------------- #
# Subcommand -> action mapping and options                                    #
# --------------------------------------------------------------------------- #

class TestSubcommandActions:

    @pytest.mark.parametrize("subcmd, action", [
        ("power", "power"),
        ("current", "current"),
        ("voltage", "voltage"),
        ("all", "all"),
    ])
    def test_action_and_role(self, subcmd, action):
        result, calls, _ = _run(["NET1", subcmd, "--box", "b"])
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert calls[0]["netname"] == "NET1"
        assert calls[0]["action"] == action
        assert calls[0]["role"] == "watt-meter"
        assert calls[0]["params"]["duration"] == 0.1

    def test_duration_forwarded(self):
        result, calls, _ = _run(
            ["NET1", "current", "--duration", "1.5", "--box", "b"]
        )
        assert result.exit_code == 0, result.output
        assert calls[0]["action"] == "current"
        assert calls[0]["params"]["duration"] == 1.5

    def test_json_output_single(self):
        result, _, _ = _run(["NET1", "current", "--json", "--box", "b"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {
            "netname": "NET1", "current": 0.0123, "duration_s": 0.1,
        }

    def test_json_output_all(self):
        result, _, _ = _run(["NET1", "all", "--json", "--box", "b"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {
            "netname": "NET1", "current": 0.1, "voltage": 3.3, "power": 0.33,
            "duration_s": 0.1,
        }

    def test_duration_short_flag(self):
        _, calls, _ = _run(["NET1", "all", "-d", "2", "--box", "b"])
        assert calls[0]["params"]["duration"] == 2.0
        assert calls[0]["action"] == "all"


# --------------------------------------------------------------------------- #
# Backward-compatible default behaviors                                       #
# --------------------------------------------------------------------------- #

class TestBackwardCompatible:

    def test_bare_net_reads_power(self):
        result, calls, _ = _run(["NET1", "--box", "b"])
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert calls[0]["action"] == "power"
        assert calls[0]["netname"] == "NET1"

    def test_no_net_lists_nets(self):
        result, calls, display_mock = _run(["--box", "b"])
        assert result.exit_code == 0, result.output
        assert calls == []
        assert display_mock.called

    def test_box_before_net_with_subcommand(self):
        result, calls, _ = _run(["--box", "b", "NET1", "current"])
        assert result.exit_code == 0, result.output
        assert calls[0]["action"] == "current"
        assert calls[0]["netname"] == "NET1"

    def test_box_before_net_no_subcommand_reads_power(self):
        result, calls, display_mock = _run(["--box", "b", "NET1"])
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert calls[0]["action"] == "power"
        assert calls[0]["netname"] == "NET1"
        assert not display_mock.called

    def test_bare_watt_reads_configured_default_net(self):
        calls: list[dict] = []

        def fake_post(ctx, box_ip, netname, action, role=None, quiet=False, **params):
            calls.append({"netname": netname, "action": action})
            return {"success": True, "value": _VALUES[action], "message": "ok"}

        display_mock = MagicMock()
        with patch.object(watt_mod, "post_net_command", fake_post), \
             patch.object(watt_mod, "resolve_box", lambda ctx, box: "1.2.3.4"), \
             patch.object(watt_mod, "validate_net_exists",
                          lambda ctx, ip, name, role: {"name": name}), \
             patch.object(watt_mod, "display_nets", display_mock), \
             patch.object(watt_mod, "get_default_net", lambda ctx, t: "defnet"):
            result = CliRunner().invoke(
                watt_group, ["--box", "b"], obj=_Obj(), catch_exceptions=False
            )
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert calls[0]["action"] == "power"
        assert calls[0]["netname"] == "defnet"
        assert not display_mock.called


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
