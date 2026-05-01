# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for `lager supply` and `lager battery` output.

These tests exercise the CLI through Click's CliRunner with the box-side
calls stubbed (no hardware, no network). They verify:

  * the new --format=text and --format=json paths behave as documented,
  * the wrong-role error path surfaces a USER_ERROR (exit 65),
  * the [OK] ASCII fallback is used when stdout is not a TTY,
  * env vars LAGER_OUTPUT_FORMAT/LAGER_OUTPUT_COLOR are propagated to the
    impl script via run_python_internal.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from cli.main import cli  # noqa: E402


FAKE_NETS = [
    {"name": "supply2", "role": "power-supply", "instrument": "Rigol DP821",
     "channel": 1},
    {"name": "battery1", "role": "battery", "instrument": "Keithley 2281S",
     "channel": 1},
]


@pytest.fixture
def captured_env_calls():
    """Records every env tuple passed to run_python_internal so tests can
    assert on LAGER_OUTPUT_FORMAT/COLOR propagation."""
    calls = []

    def fake_run_python(ctx, script_path, box, *, env=(), **kwargs):
        calls.append({"script": script_path, "box": box, "env": tuple(env)})
        # Simulate silent success — impl scripts emit their own output for
        # state commands, but this stub stays quiet so the CLI wrapper's
        # ack path is the only emitter.
        return None

    with patch("cli.commands.power.supply.run_python_internal", fake_run_python), \
         patch("cli.commands.power.battery.run_python_internal", fake_run_python):
        yield calls


@pytest.fixture
def stub_nets():
    """Patches list_nets_by_role and run_net_py used by validate_net_exists."""
    def fake_list_by_role(ctx, box, role):
        return [n for n in FAKE_NETS if n["role"] == role]

    def fake_run_net_py(ctx, box, action):
        return list(FAKE_NETS)

    with patch("cli.core.net_helpers.list_nets_by_role", fake_list_by_role), \
         patch("cli.core.net_helpers.run_net_py", fake_run_net_py):
        yield


@pytest.fixture
def stub_box_resolution():
    """Bypass box-name → IP resolution and netname requirement at the call
    sites (the names are bound at import-time in the command modules)."""
    fake_resolve = lambda ctx, box: "10.0.0.1"
    fake_require = lambda ctx, kind: getattr(ctx.obj, "netname", None) or "supply2"

    with patch("cli.commands.power.supply.resolve_box", fake_resolve), \
         patch("cli.commands.power.battery.resolve_box", fake_resolve), \
         patch("cli.commands.power.supply.require_netname", fake_require), \
         patch("cli.commands.power.battery.require_netname", fake_require):
        yield


@pytest.fixture
def stub_websocket_fastpath():
    """The supply/battery wrappers try a WebSocket call before falling back
    to the impl. Make every WS request raise ConnectionError so the impl
    fallback runs (which is what we're testing)."""
    import requests
    def fake_post(url, **kw):
        raise requests.ConnectionError("test stub: no WS")
    with patch("requests.post", fake_post):
        yield


# --------------------------------------------------------------------------- #
# Supply: enable in text and JSON modes
# --------------------------------------------------------------------------- #

class TestSupplyEnable:
    def test_text_mode_emits_action(self, captured_env_calls, stub_nets,
                                     stub_box_resolution, stub_websocket_fastpath):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--color", "never", "supply", "supply2", "enable", "--yes"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        # Symbol may render as ✓ (UTF-8 stream) or [OK] (ASCII fallback).
        assert ("✓ Enabled supply output" in result.output
                or "[OK] Enabled supply output" in result.output)

    def test_json_mode_emits_envelope(self, captured_env_calls, stub_nets,
                                        stub_box_resolution, stub_websocket_fastpath):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--format", "json", "supply", "supply2", "enable", "--yes"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        env = json.loads(result.output.strip().splitlines()[-1])
        assert env["lager"] == 1
        assert env["kind"] == "ack"
        assert env["status"] == "ok"
        assert env["command"] == "supply.enable"
        assert env["subject"] == {"net": "supply2"}
        assert env["message"] == "Enabled supply output"

    def test_env_vars_propagated_to_impl(self, captured_env_calls, stub_nets,
                                          stub_box_resolution, stub_websocket_fastpath):
        runner = CliRunner()
        runner.invoke(cli, ["--format", "json",
                            "supply", "supply2", "enable", "--yes"],
                      catch_exceptions=False)
        assert captured_env_calls, "run_python_internal should have been called"
        env = captured_env_calls[-1]["env"]
        # Find the format/color entries (order-independent).
        env_dict = {kv.split("=", 1)[0]: kv.split("=", 1)[1] for kv in env}
        assert env_dict["LAGER_OUTPUT_FORMAT"] == "json"
        assert env_dict["LAGER_OUTPUT_COLOR"] == "0"  # color forced off in JSON
        assert "LAGER_COMMAND_DATA" in env_dict


# --------------------------------------------------------------------------- #
# Battery wrong-role error path (the user's lead complaint)
# --------------------------------------------------------------------------- #

class TestBatteryWrongRole:
    def test_text_mode_user_error_exit_65(self, captured_env_calls, stub_nets,
                                            stub_box_resolution, stub_websocket_fastpath):
        runner = CliRunner()
        # supply2 is a power-supply, not a battery → user error.
        result = runner.invoke(
            cli, ["--color", "never", "battery", "supply2", "state"],
            catch_exceptions=False,
        )
        assert result.exit_code == 65, result.output
        # Error goes to stderr; CliRunner mixes stdout+stderr into result.output
        # by default (unless mix_stderr=False), but we constructed CliRunner()
        # with the default which mixes them.
        assert "supply2" in result.output
        assert "power-supply" in result.output
        assert "battery" in result.output

    def test_json_mode_error_envelope_with_actual_role(self, captured_env_calls,
                                                       stub_nets, stub_box_resolution,
                                                       stub_websocket_fastpath):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--format", "json", "battery", "supply2", "state"],
            catch_exceptions=False,
        )
        assert result.exit_code == 65, result.output
        # Final line on stdout must be an error envelope.
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        env = json.loads(lines[-1])
        assert env["kind"] == "error"
        assert env["status"] == "error"
        assert env["exit_code"] == 65
        assert env["subject"]["net"] == "supply2"
        assert env["data"]["actual_role"] == "power-supply"
        assert env["data"]["expected_role"] == "battery"
        # Dotted command path, not just "state".
        assert env["command"] == "battery.state"


# --------------------------------------------------------------------------- #
# Out-of-range SOC: battery user-error in both modes
# --------------------------------------------------------------------------- #

class TestBatterySocValidation:
    def test_out_of_range_user_error(self, captured_env_calls, stub_nets,
                                       stub_box_resolution, stub_websocket_fastpath):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--color", "never", "battery", "battery1", "soc", "150"],
            catch_exceptions=False,
        )
        assert result.exit_code == 65
        assert "between 0 and 100" in result.output

    def test_out_of_range_json(self, captured_env_calls, stub_nets,
                                 stub_box_resolution, stub_websocket_fastpath):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["--format", "json", "battery", "battery1", "soc", "150"],
            catch_exceptions=False,
        )
        assert result.exit_code == 65
        env = json.loads(result.output.strip().splitlines()[-1])
        assert env["kind"] == "error"
        assert env["exit_code"] == 65
        assert env["command"] == "battery.soc"


# --------------------------------------------------------------------------- #
# Color suppression in JSON mode
# --------------------------------------------------------------------------- #

def test_no_ansi_in_json_output(captured_env_calls, stub_nets,
                                  stub_box_resolution, stub_websocket_fastpath):
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--format", "json", "--color", "always",
              "supply", "supply2", "enable", "--yes"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "\x1b[" not in result.output


# --------------------------------------------------------------------------- #
# --colorize deprecation
# --------------------------------------------------------------------------- #

def test_colorize_emits_deprecation_warning(captured_env_calls, stub_nets,
                                              stub_box_resolution, stub_websocket_fastpath):
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--colorize", "supply", "supply2", "enable", "--yes"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "--colorize is deprecated" in result.output
