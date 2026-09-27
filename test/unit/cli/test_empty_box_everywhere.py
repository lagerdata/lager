# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Every command refuses `--box ""` instead of using the default box (#512).

`test_empty_box_name.py` covers the shared resolvers, which have refused an
empty name since v0.41.0. Several commands never reached them with the empty
string: they tested `if not box:` or `box or <group value>` first, and an
empty string is falsy, so they swapped in the default box before resolving.
In CI, `--box "$BOX"` with `BOX` unset then ran against whatever box was the
default.

These tests drive each such command through click with `--box ""` and check
that it exits with the resolver's message and never asks for the default box.
The AST guard at the bottom fails on a new `if not box: box = ...` or
`x = box or ...` fallback anywhere under `cli/commands`.
"""

import ast
import importlib
import pathlib
import types
from unittest import mock

import pytest
from click.testing import CliRunner

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
COMMANDS = REPO_ROOT / "cli" / "commands"
MESSAGE = "Box name cannot be empty"


def _mod(name):
    return importlib.import_module(name)


@pytest.fixture(autouse=True)
def no_default_box_and_no_io(monkeypatch):
    """Asking for the default box is the bug. Any I/O means the guard missed."""
    def default_box(*args, **kwargs):
        raise AssertionError("the command fell back to the default box")

    def forbidden(*args, **kwargs):
        raise AssertionError("the command reached the network or a subprocess")

    context_core = _mod("cli.context.core")
    monkeypatch.setattr(context_core, "get_default_box", default_box)
    for name in ("cli.context", "cli.commands.utility.update", "cli.commands.box.ssh",
                 "cli.commands.utility.binaries", "cli.commands.box.config"):
        module = _mod(name)
        if hasattr(module, "get_default_box"):
            monkeypatch.setattr(module, "get_default_box", default_box)
    for verb in ("get", "post", "put", "delete", "request"):
        monkeypatch.setattr(f"requests.{verb}", forbidden, raising=False)
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("subprocess.call", forbidden)


def _obj():
    return types.SimpleNamespace(default_box=None, netname=None, debug=False)


def _invoke(module_name, attr, argv):
    command = getattr(_mod(module_name), attr)
    return CliRunner().invoke(command, argv, obj=_obj())


CASES = [
    ("cli.commands.utility.update", "update", ["--box", "", "--yes"]),
    ("cli.commands.box.ssh", "ssh", ["--box", ""]),
    ("cli.commands.utility.binaries", "binaries", ["list", "--box", ""]),
    ("cli.commands.utility.binaries", "binaries", ["remove", "tool", "--box", "", "--yes"]),
    ("cli.commands.box.nets", "nets", ["--box", ""]),
    ("cli.commands.box.nets", "nets", ["state", "--box", ""]),
    ("cli.commands.box.config", "box_config", ["--box", ""]),
    ("cli.commands.box.config", "box_config", ["show", "--box", ""]),
    ("cli.commands.development.arm", "arm", ["--box", "", "arm1", "position"]),
    ("cli.commands.development.arm", "arm", ["arm1", "position", "--box", ""]),
    ("cli.commands.communication.spi", "spi", ["spi1", "config", "--box", ""]),
    ("cli.commands.communication.spi", "spi", ["--box", "", "spi1", "config"]),
    ("cli.commands.communication.i2c", "i2c", ["i2c1", "scan", "--box", ""]),
    ("cli.commands.communication.i2c", "i2c", ["--box", "", "i2c1", "scan"]),
    ("cli.commands.utility.logs", "logs", ["size", "--box", ""]),
    ("cli.commands.utility.logs", "logs", ["clean", "--box", "", "--yes"]),
    ("cli.commands.utility.logs", "logs", ["docker", "--box", ""]),
    ("cli.commands.utility.install", "install", ["--box", "", "--yes"]),
    ("cli.commands.utility.uninstall", "uninstall", ["--box", "", "--yes"]),
]


@pytest.mark.parametrize("module_name, attr, argv", CASES,
                         ids=[f"{m.rsplit('.', 1)[1]}:{' '.join(a)}" for m, _, a in CASES])
def test_an_empty_box_is_refused(module_name, attr, argv):
    result = _invoke(module_name, attr, argv)
    assert result.exit_code != 0, result.output
    assert MESSAGE in result.output, result.output


def test_binaries_add_refuses_an_empty_box(tmp_path):
    binary = tmp_path / "tool"
    binary.write_bytes(b"\x7fELF")
    result = _invoke("cli.commands.utility.binaries", "binaries",
                     ["add", str(binary), "--box", "", "--yes"])
    assert result.exit_code != 0, result.output
    assert MESSAGE in result.output, result.output


def test_a_blank_box_is_refused_too():
    result = _invoke("cli.commands.box.ssh", "ssh", ["--box", "   "])
    assert result.exit_code != 0
    assert MESSAGE in result.output


def test_a_group_level_box_still_reaches_the_subcommand():
    """The `is not None` fallback must keep passing a real group value on."""
    spi = _mod("cli.commands.communication.spi")
    with mock.patch.object(spi, "resolve_box_locked", side_effect=SystemExit(9)) as resolve:
        CliRunner().invoke(spi.spi, ["--box", "bench-a", "spi1", "config"], obj=_obj())
    assert resolve.call_args.args[1] == "bench-a"


# ---------------------------------------------------------------------------
# No new fallback slips in
# ---------------------------------------------------------------------------

def _is_box_name(node):
    return isinstance(node, ast.Name) and (node.id == "box" or node.id.endswith(("_box", "box_opt", "box_param")))


def _fallbacks(tree):
    """The two shapes that pick a box: `if not <box>: <box> = ...` and
    `<name> = <box> or ...`.

    `<box> or ...` elsewhere (in a message, or as a lock label after the box
    is resolved) chooses nothing, so it is not flagged.
    """
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.If)
                and isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)
                and _is_box_name(node.test.operand)):
            name = node.test.operand.id
            if any(isinstance(stmt, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets)
                   for stmt in node.body):
                found.append((node.lineno, f"if not {name}: {name} = ..."))
        if (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.BoolOp) and isinstance(node.value.op, ast.Or)
                and _is_box_name(node.value.values[0])):
            found.append((node.lineno, f"... = {node.value.values[0].id} or ..."))
    return found


def test_the_walker_finds_the_shapes_it_looks_for():
    tree = ast.parse("if not box:\n    box = pick()\n"
                     "x = target_box or other\n"
                     "print(f'--box {box or ip}')\n")
    assert sorted(text for _, text in _fallbacks(tree)) == [
        "... = target_box or ...", "if not box: box = ..."]


def test_no_command_falls_back_on_an_empty_box():
    offenders = []
    for path in sorted(COMMANDS.rglob("*.py")):
        for lineno, text in _fallbacks(ast.parse(path.read_text())):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}  {text}")
    assert not offenders, (
        "an empty --box is falsy, so these treat it as 'not given' and fall "
        "back to another box. Test `is None` and let the resolver refuse the "
        "empty string:\n  " + "\n  ".join(offenders)
    )
