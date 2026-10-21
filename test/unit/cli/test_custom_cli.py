# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""`lager custom` and the `lager <type>` short form for custom drivers.

The short form is resolved by the root group from the manifest the target box
serves. These tests pin the rules that keep it safe: a built-in command name
always wins, help never touches the network, and any discovery failure is the
ordinary "No such command".
"""

from __future__ import annotations

import ast
import importlib
import json
import os
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

custom_mod = importlib.import_module("cli.commands.custom")
custom_drivers = importlib.import_module("cli.core.custom_drivers")
net_helpers = importlib.import_module("cli.core.net_helpers")
main_mod = importlib.import_module("cli.main")
from test.unit.cli.drivers_http_fake import FakeDriversBox, stepper_entry  # noqa: E402

BOX_IP = "10.0.0.5"
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(custom_drivers, "CACHE_FILE", tmp_path / "drivers_manifest.json")
    monkeypatch.delenv("LAGER_BOX", raising=False)


@pytest.fixture
def box():
    fake = FakeDriversBox(drivers=[stepper_entry()])
    fake.saved_nets = [
        {"name": "UART_1", "role": "uart", "instrument": "FTDI"},
        {"name": "RIG_1", "role": "stepper", "instrument": "custom:stepper",
         "params": {"uart_net": "UART_1"}},
    ]
    with patch("requests.request", fake.request), patch("requests.get", fake.get):
        yield fake


@pytest.fixture
def posted():
    """Capture post_net_command instead of reaching a box."""
    calls = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False,
                  http_timeout=None, **params):
        calls.append({"box": box_ip, "netname": netname, "action": action,
                      "role": role, "params": params})
        return {"success": True, "message": "jogged", "value": {"message": "jogged",
                                                                  "seconds": 3.0}}

    with patch.object(net_helpers, "post_net_command", fake_post), \
            patch.object(custom_mod, "post_net_command", fake_post), \
            patch.object(net_helpers, "resolve_box_locked", lambda ctx, box, name: BOX_IP), \
            patch.object(custom_mod, "resolve_box_locked", lambda ctx, box, name: BOX_IP), \
            patch.object(custom_mod, "resolve_box", lambda ctx, box, read_only=False: BOX_IP), \
            patch.object(net_helpers, "resolve_box", lambda ctx, box, read_only=False: BOX_IP), \
            patch.object(net_helpers, "validate_net_exists", lambda *a, **k: {}):
        yield calls


@pytest.fixture
def known_box():
    with patch("cli.box_storage.get_box_ip", lambda name: BOX_IP if name == "B" else None):
        yield


# ---------------------------------------------------------------------------
# parse_action_args
# ---------------------------------------------------------------------------

class TestParseActionArgs:
    def test_forms(self):
        out = custom_mod.parse_action_args(
            ["--seconds", "3", "--hold-time=2", "--fast", "--no-home", "--mode", "b"])
        assert out == {"seconds": "3", "hold_time": "2", "fast": "true",
                       "home": "false", "mode": "b"}

    def test_flag_before_flag_is_true(self):
        assert custom_mod.parse_action_args(["--fast", "--seconds", "1"]) == \
            {"fast": "true", "seconds": "1"}

    @pytest.mark.parametrize("tokens", [["3"], ["--seconds", "1", "extra"], ["--"], ["--=x"]])
    def test_rejects_stray_values(self, tokens):
        with pytest.raises(click.UsageError):
            custom_mod.parse_action_args(tokens)


# ---------------------------------------------------------------------------
# lager custom
# ---------------------------------------------------------------------------

class TestCustomCommand:
    def test_lists_custom_nets(self, box, posted):
        result = CliRunner().invoke(custom_mod.custom, ["--box", "B"])
        assert result.exit_code == 0, result.output
        assert "RIG_1" in result.output and "stepper" in result.output

    def test_lists_a_nets_actions(self, box, posted):
        result = CliRunner().invoke(custom_mod.custom, ["RIG_1", "--box", "B"])
        assert result.exit_code == 0, result.output
        assert "jog" in result.output and "--seconds <float>" in result.output

    def test_unknown_net(self, box, posted):
        result = CliRunner().invoke(custom_mod.custom, ["NOPE", "--box", "B"])
        assert result.exit_code == 1
        assert "not a custom net" in result.output

    def test_runs_action_with_string_args(self, box, posted):
        result = CliRunner().invoke(custom_mod.custom,
                                    ["RIG_1", "jog", "--seconds", "3", "--box", "B"])
        assert result.exit_code == 0, result.output
        assert posted == [{"box": BOX_IP, "netname": "RIG_1", "action": "jog",
                           "role": None, "params": {"seconds": "3"}}]
        assert "jogged" in result.output and "seconds: 3.0" in result.output

    def test_json_output(self, box, posted):
        result = CliRunner().invoke(custom_mod.custom,
                                    ["RIG_1", "jog", "--seconds=3", "--json", "--box", "B"])
        assert json.loads(result.output) == {"message": "jogged", "seconds": 3.0}


# ---------------------------------------------------------------------------
# lager <type> short form, through the real root group
# ---------------------------------------------------------------------------

def _root(args):
    return CliRunner().invoke(main_mod.cli, args)


class TestShortForm:
    def test_resolves_from_box_and_sends_typed_args(self, box, posted, known_box):
        result = _root(["stepper", "RIG_1", "jog", "--seconds", "3", "--fast", "--box", "B"])
        assert result.exit_code == 0, result.output
        assert posted[0]["role"] == "stepper"
        assert posted[0]["params"] == {"seconds": 3.0, "hold_time": 3.0, "fast": True,
                                       "mode": "a"}

    def test_box_equals_form(self, box, posted, known_box):
        result = _root(["stepper", "RIG_1", "home", "--box=B"])
        assert result.exit_code == 0, result.output
        assert posted[0]["action"] == "home"

    def test_default_box(self, box, posted, known_box, monkeypatch):
        monkeypatch.setenv("LAGER_BOX", "B")
        result = _root(["stepper", "RIG_1", "home"])
        assert result.exit_code == 0, result.output

    def test_click_validates_ranges_before_sending(self, box, posted, known_box):
        result = _root(["stepper", "RIG_1", "jog", "--seconds", "99", "--box", "B"])
        assert result.exit_code == 2
        assert posted == []

    def test_help_is_built_from_the_manifest(self, box, posted, known_box):
        result = _root(["stepper", "RIG_1", "jog", "--help", "--box", "B"])
        assert result.exit_code == 0, result.output
        assert "--seconds" in result.output and "--hold-time" in result.output
        assert "--no-fast" in result.output

    def test_cache_hit_needs_no_request(self, box, posted, known_box):
        assert _root(["stepper", "RIG_1", "home", "--box", "B"]).exit_code == 0
        box.calls.clear()
        assert _root(["stepper", "RIG_1", "home", "--box", "B"]).exit_code == 0
        assert not [c for c in box.calls if c[1] == "/drivers"]

    def test_stale_cache_without_the_type_is_refreshed(self, box, posted, known_box):
        custom_drivers.remember_drivers(BOX_IP, [])
        result = _root(["stepper", "RIG_1", "home", "--box", "B"])
        assert result.exit_code == 0, result.output

    def test_unknown_type_is_no_such_command(self, box, posted, known_box):
        result = _root(["nosuch", "RIG_1", "--box", "B"])
        assert result.exit_code == 2
        assert "No such command 'nosuch'" in result.output

    def test_unreachable_box_is_no_such_command(self, posted, known_box):
        import requests

        def boom(*a, **k):
            raise requests.ConnectionError("down")

        with patch("requests.get", boom):
            result = _root(["stepper", "RIG_1", "--box", "B"])
        assert result.exit_code == 2
        assert "No such command 'stepper'" in result.output

    def test_typo_asks_the_box_once(self, box, posted, known_box):
        result = _root(["nosuch", "RIG_1", "--box", "B"])
        assert result.exit_code == 2
        assert len([c for c in box.calls if c[1] == "/drivers"]) == 1

    def test_repeated_typo_is_answered_from_the_cache(self, box, posted, known_box):
        _root(["nosuch", "RIG_1", "--box", "B"])
        box.calls.clear()
        assert _root(["nosuch", "RIG_1", "--box", "B"]).exit_code == 2
        assert not [c for c in box.calls if c[1] == "/drivers"]

    def test_a_second_typo_keeps_the_first_miss(self, box, posted, known_box):
        _root(["nosuch", "RIG_1", "--box", "B"])
        _root(["nosuch2", "RIG_1", "--box", "B"])
        box.calls.clear()
        assert _root(["nosuch", "RIG_1", "--box", "B"]).exit_code == 2
        assert not [c for c in box.calls if c[1] == "/drivers"]

    def test_missed_name_is_asked_again_after_miss_ttl(self, box, posted, known_box,
                                                         monkeypatch):
        _root(["stepper2", "RIG_1", "--box", "B"])
        box.drivers["stepper2"] = stepper_entry(type="stepper2")
        monkeypatch.setattr(custom_drivers, "MISS_TTL", 0)
        result = _root(["stepper2", "RIG_1", "home", "--box", "B"])
        assert result.exit_code == 0, result.output

    def test_unreachable_box_is_asked_once_then_remembered(self, posted, known_box):
        import requests

        calls = []

        def boom(*a, **k):
            calls.append(a)
            raise requests.ConnectionError("down")

        with patch("requests.get", boom):
            assert _root(["stepper", "RIG_1", "--box", "B"]).exit_code == 2
            assert len(calls) == 1
            assert _root(["stepper", "RIG_1", "--box", "B"]).exit_code == 2
        assert len(calls) == 1

    def test_no_box_at_all_is_no_such_command(self, posted):
        with patch("cli.config.read_config_file", side_effect=Exception):
            result = _root(["stepper", "RIG_1"])
        assert result.exit_code == 2

    def test_builtin_name_is_never_shadowed(self, box, posted, known_box):
        box.drivers["uart"] = stepper_entry(type="uart")
        with patch.object(custom_drivers, "custom_driver_command") as discover:
            _root(["uart", "--help"])
        discover.assert_not_called()

    def test_root_help_never_touches_the_network(self, posted, known_box):
        with patch("requests.get") as get, patch("requests.request") as req:
            result = _root(["--help"])
        assert result.exit_code == 0
        get.assert_not_called()
        req.assert_not_called()
        assert "drivers" in result.output and "custom" in result.output


# ---------------------------------------------------------------------------
# Reserved names: the box must refuse a driver type that collides with the CLI
# ---------------------------------------------------------------------------

def _box_reserved_types():
    """RESERVED_TYPES from box/lager/drivers/manifest.py, read without importing box code."""
    path = os.path.join(REPO, "box", "lager", "drivers", "manifest.py")
    tree = ast.parse(open(path).read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "RESERVED_TYPES" for t in node.targets):
            return set(ast.literal_eval(node.value.args[0]))
    raise AssertionError("RESERVED_TYPES not found")


def _net_type_roles():
    path = os.path.join(REPO, "box", "lager", "nets", "constants.py")
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "from_role":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    return {ast.literal_eval(k) for k in sub.keys}
    raise AssertionError("NetType.from_role mapping not found")


class TestReservedNamesSync:
    def test_every_top_level_command_is_reserved(self):
        missing = set(main_mod.cli.list_commands(None)) - _box_reserved_types()
        assert not missing, (
            f"Add {sorted(missing)} to RESERVED_TYPES in box/lager/drivers/manifest.py, "
            f"or a custom driver could take a built-in command's name")

    def test_every_net_role_is_reserved(self):
        roles = _net_type_roles() | set(net_helpers.NET_ROLES.values())
        missing = roles - _box_reserved_types()
        assert not missing, f"Add {sorted(missing)} to RESERVED_TYPES"
