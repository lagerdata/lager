# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager defaults` -- add, list, delete and delete-all, against a real `.lager`.

`cli/commands/utility/defaults.py` had no test. It writes the user's global
config, and every net command reads its fallback net from what it writes, so
the two ends are tested together here: a default is set through the command
and read back through `get_default_net`, the function the net commands call.
That is the check that found the watt-meter key mismatch pinned below.

Everything runs in a temp config dir (`fake_box` sets LAGER_CONFIG_FILE_DIR
and chdirs into it), so no developer config is read or written.
"""

import ast
import importlib
import json
import pathlib
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# By module path: the package re-exports the click group under the same name.
defaults_mod = importlib.import_module("cli.commands.utility.defaults")


@pytest.fixture
def lager_file(fake_box, tmp_path):
    path = tmp_path / ".lager"
    path.write_text(json.dumps({"BOXES": {"bench-a": {"ip": "192.0.2.10", "user": "benchuser"}}}))
    return path


def _saved(path):
    return json.loads(path.read_text())


def _default_net(net_type):
    from cli.context import get_default_net
    return get_default_net(types.SimpleNamespace(), net_type)


def test_no_defaults_says_how_to_add_one(lager, lager_file):
    result = lager("defaults")
    assert result.exit_code == 0, result.output
    assert "No defaults are currently set" in result.output


def test_add_with_no_option_writes_nothing(lager, lager_file):
    before = lager_file.read_text()
    result = lager("defaults", "add")
    assert result.exit_code != 0
    assert "You must specify at least one setting" in result.output
    assert lager_file.read_text() == before


def test_add_then_list_round_trip(lager, lager_file):
    result = lager("defaults", "add", "--supply-net", "supply1", "--scope-net", "scope1")
    assert result.exit_code == 0, result.output
    assert "Set defaults:" in result.output
    listed = lager("defaults", "list")
    assert listed.exit_code == 0, listed.output
    assert "supply-net" in listed.output and "supply1" in listed.output
    assert "scope-net" in listed.output and "scope1" in listed.output


def test_add_keeps_the_saved_boxes(lager, lager_file):
    lager("defaults", "add", "--supply-net", "supply1")
    assert _saved(lager_file)["BOXES"] == {"bench-a": {"ip": "192.0.2.10", "user": "benchuser"}}


def test_a_default_box_must_be_a_saved_box(lager, lager_file):
    result = lager("defaults", "add", "--box", "bench-z")
    assert result.exit_code != 0
    assert "Box 'bench-z' not found in saved boxes" in result.output
    assert "bench-a" in result.output
    assert "LAGER" not in _saved(lager_file) and "DEFAULTS" not in _saved(lager_file)


def test_a_saved_box_becomes_the_default(lager, lager_file):
    result = lager("defaults", "add", "--box", "bench-a")
    assert result.exit_code == 0, result.output
    assert "Set default box: bench-a" in result.output
    assert "bench-a" in lager("defaults").output


@pytest.mark.parametrize("option, net_type", [
    ("--supply-net", "power_supply"),
    ("--battery-net", "battery"),
    ("--solar-net", "solar"),
    ("--scope-net", "scope"),
    ("--logic-net", "logic"),
    ("--adc-net", "adc"),
    ("--dac-net", "dac"),
    ("--gpio-net", "gpio"),
    ("--debug-net", "debug"),
    ("--eload-net", "eload"),
    ("--usb-net", "usb"),
    ("--webcam-net", "webcam"),
    ("--thermocouple-net", "thermocouple"),
    ("--uart-net", "uart"),
    ("--arm-net", "arm"),
    pytest.param("--watt-meter-net", "watt", marks=pytest.mark.xfail(
        strict=True,
        reason="defaults add --watt-meter-net writes net_watt_meter, but `lager watt` "
               "reads get_default_net(ctx, 'watt') = net_watt, so the default is "
               "never used",
    )),
])
def test_a_net_default_is_the_one_its_command_reads(lager, lager_file, option, net_type):
    result = lager("defaults", "add", option, "net-under-test")
    assert result.exit_code == 0, result.output
    assert _default_net(net_type) == "net-under-test"


def _net_types_read_by_commands():
    """Every literal net type passed to get_default_net under cli/commands."""
    found = set()
    for path in (REPO_ROOT / "cli" / "commands").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "get_default_net"
                    and len(node.args) == 2 and isinstance(node.args[1], ast.Constant)):
                found.add(node.args[1].value)
    return found


def test_net_types_no_defaults_option_can_set():
    """Two-sided: a command reads these defaults, but `defaults add` cannot set them.

    They can only be set by hand-editing `.lager`. Shrink this set by adding
    the option; a new command that reads an unsettable default fails here.
    """
    settable = {key[len("net_"):] for _, key, _ in defaults_mod._net_types}
    assert _net_types_read_by_commands() - settable == {"energy", "i2c", "spi", "watt"}


def test_every_add_option_has_a_delete_subcommand():
    group = defaults_mod.defaults.commands["delete"]
    add_opts = {p.name.replace("_", "-") for p in defaults_mod.defaults.commands["add"].params}
    assert add_opts - {"serial-port"} <= set(group.commands) | {"box", "user"}
    assert "serial-port" in group.commands


def test_delete_one_net_default(lager, lager_file):
    lager("defaults", "add", "--supply-net", "supply1", "--adc-net", "adc1")
    result = lager("defaults", "delete", "supply-net", "--yes")
    assert result.exit_code == 0, result.output
    assert "Deleted default supply net 'supply1'" in result.output
    assert _default_net("power_supply") is None
    assert _default_net("adc") == "adc1"


def test_delete_declined_keeps_the_default(lager, lager_file):
    lager("defaults", "add", "--supply-net", "supply1")
    result = lager("defaults", "delete", "supply-net", input="n\n")
    assert "Cancelled" in result.output
    assert _default_net("power_supply") == "supply1"


def test_delete_an_unset_default_says_so(lager, lager_file):
    result = lager("defaults", "delete", "debug-net", "--yes")
    assert result.exit_code == 0
    assert "No default debug net is currently set" in result.output


@pytest.mark.parametrize("sub, option, value", [
    ("box", "--box", "bench-a"),
    ("serial-port", "--serial-port", "/dev/ttyUSB0"),
    ("user", "--user", "benchuser@example.com"),
])
def test_delete_the_non_net_defaults(lager, lager_file, sub, option, value):
    lager("defaults", "add", option, value)
    assert value in lager("defaults").output
    result = lager("defaults", "delete", sub, "--yes")
    assert result.exit_code == 0, result.output
    assert value not in lager("defaults").output


def test_delete_all_clears_defaults_but_not_boxes(lager, lager_file):
    lager("defaults", "add", "--supply-net", "supply1", "--user", "benchuser")
    result = lager("defaults", "delete-all", "--yes")
    assert result.exit_code == 0, result.output
    assert "Deleted all 2 default(s)" in result.output
    assert "No defaults are currently set" in lager("defaults").output
    assert "bench-a" in _saved(lager_file)["BOXES"]


def test_delete_all_declined_keeps_everything(lager, lager_file):
    lager("defaults", "add", "--supply-net", "supply1")
    result = lager("defaults", "delete-all", input="n\n")
    assert "Cancelled. No defaults were deleted." in result.output
    assert _default_net("power_supply") == "supply1"
