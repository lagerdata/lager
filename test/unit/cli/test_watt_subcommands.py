#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the `lager watt` net group (cli/commands/measurement/watt.py).

The watt command became a NetGroup so it can read current/voltage/all in
addition to power. These tests assert the CLI ships the right payload to the
box-side impl (mode/duration/json), and that the original behaviors —
`lager watt NET` reads power and `lager watt` lists nets — are preserved.

The box is mocked at the `run_python_internal` boundary, so no hardware or
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


def _run(args):
    """Invoke the watt group with the box boundary mocked; return (result, payloads)."""
    payloads: list[dict] = []

    def fake_run_python_internal(*, args=(), **kwargs):
        # The impl is shipped a single JSON payload as args[0].
        payloads.append(json.loads(args[0]))
        return 0

    display_mock = MagicMock()

    with patch.object(watt_mod, "run_python_internal", fake_run_python_internal), \
         patch.object(watt_mod, "resolve_box", lambda ctx, box: "1.2.3.4"), \
         patch.object(watt_mod, "validate_net_exists",
                      lambda ctx, ip, name, role: {"name": name}), \
         patch.object(watt_mod, "display_nets", display_mock), \
         patch.object(watt_mod, "get_default_net", lambda ctx, t: None):
        result = CliRunner().invoke(
            watt_group, args, obj=_Obj(), catch_exceptions=False
        )
    return result, payloads, display_mock


# --------------------------------------------------------------------------- #
# Subcommand payloads                                                         #
# --------------------------------------------------------------------------- #

class TestSubcommandPayloads:

    @pytest.mark.parametrize("subcmd, mode", [
        ("power", "power"),
        ("current", "current"),
        ("voltage", "voltage"),
        ("all", "all"),
    ])
    def test_mode_in_payload(self, subcmd, mode):
        result, payloads, _ = _run(["NET1", subcmd, "--box", "b"])
        assert result.exit_code == 0, result.output
        assert len(payloads) == 1
        assert payloads[0]["netname"] == "NET1"
        assert payloads[0]["mode"] == mode
        assert payloads[0]["duration"] == 0.1
        assert payloads[0]["json"] is False

    def test_duration_and_json_forwarded(self):
        result, payloads, _ = _run(
            ["NET1", "current", "--duration", "1.5", "--json", "--box", "b"]
        )
        assert result.exit_code == 0, result.output
        assert payloads[0] == {
            "netname": "NET1", "mode": "current", "duration": 1.5, "json": True,
        }

    def test_duration_short_flag(self):
        _, payloads, _ = _run(["NET1", "all", "-d", "2", "--box", "b"])
        assert payloads[0]["duration"] == 2.0
        assert payloads[0]["mode"] == "all"


# --------------------------------------------------------------------------- #
# Backward-compatible default behaviors                                       #
# --------------------------------------------------------------------------- #

class TestBackwardCompatible:

    def test_bare_net_reads_power(self):
        # `lager watt NET --box b` (no subcommand) still reads power.
        result, payloads, _ = _run(["NET1", "--box", "b"])
        assert result.exit_code == 0, result.output
        assert len(payloads) == 1
        assert payloads[0]["mode"] == "power"
        assert payloads[0]["netname"] == "NET1"

    def test_no_net_lists_nets(self):
        # `lager watt --box b` (no net, no subcommand) lists nets, no read.
        result, payloads, display_mock = _run(["--box", "b"])
        assert result.exit_code == 0, result.output
        assert payloads == []
        assert display_mock.called

    def test_bare_net_power_accepts_flags(self):
        # `lager watt NET --duration 2 --json --box b` -> power read with flags,
        # via the injected default `power` subcommand.
        result, payloads, _ = _run(
            ["NET1", "--duration", "2", "--json", "--box", "b"]
        )
        assert result.exit_code == 0, result.output
        assert payloads[0] == {
            "netname": "NET1", "mode": "power", "duration": 2.0, "json": True,
        }

    def test_box_before_net_with_subcommand(self):
        # `lager watt --box b NET current` -> current; the group-level --box is
        # picked up via the ctx fallback.
        result, payloads, _ = _run(["--box", "b", "NET1", "current"])
        assert result.exit_code == 0, result.output
        assert payloads[0]["mode"] == "current"
        assert payloads[0]["netname"] == "NET1"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
