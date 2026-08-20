# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Every `lager logic` action must be handled by the script it dispatches to.

`test_impl_script_dispatch.py` asserts the script *file* exists. That is not the
contract that broke in #261: `measurement.py`, `trigger.py` and `cursor.py` were
consolidated into `scope.py`, and `logic.py` kept naming the old files. A
filename check would have caught that -- but it would not catch the inverse and
more likely drift, where the file exists and simply does not handle the action
sent to it. Sixteen subcommands were dead for exactly one release cycle without
anything failing at the call site, so the check has to be on the action name.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[3] / 'cli'
LOGIC = CLI / 'commands' / 'measurement' / 'logic.py'
IMPL = CLI / 'impl'


def _impl_path(script: str) -> Path:
    """Mirror get_impl_path's search: the subdirectories, then impl/ root."""
    for sub in ('power', 'measurement', 'communication', 'device'):
        candidate = IMPL / sub / script
        if candidate.exists():
            return candidate
    return IMPL / script


def _script_for_each_helper() -> dict[str, str]:
    """Map `_run_*_backend` -> the impl script literal it passes."""
    tree = ast.parse(LOGIC.read_text())
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith('_run'):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == 'run_backend'):
                for arg in call.args:
                    if isinstance(arg, ast.Constant) and str(arg.value).endswith('.py'):
                        out[node.name] = arg.value
    return out


def _dispatched_actions() -> set[tuple[str, str]]:
    """Every (script, action) pair `lager logic` can send."""
    helpers = _script_for_each_helper()
    tree = ast.parse(LOGIC.read_text())
    pairs = set()
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
            continue
        script = helpers.get(call.func.id)
        if script is None:
            continue
        for arg in call.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if not arg.value.endswith('.py'):
                    pairs.add((script, arg.value))
                    break
    return pairs


def _actions_handled(script_path: Path) -> set[str]:
    """Action names an impl script responds to.

    Covers the three shapes these scripts use: dict dispatch tables, an
    `action == 'x'` chain, and `action in [...]` membership.
    """
    tree = ast.parse(script_path.read_text())
    handled: set[str] = set()

    for node in ast.walk(tree):
        # `foo_actions = {'name': ...}` / `basic_operations = {...}`
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(n.endswith(('_actions', '_operations')) for n in names):
                handled |= {k.value for k in node.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        # `action == 'x'` and `action in ('x', 'y')`
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) \
                and node.left.id == 'action':
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant):
                    handled.add(comp.value)
                elif isinstance(op, ast.In) and isinstance(comp, (ast.List, ast.Tuple, ast.Set)):
                    handled |= {e.value for e in comp.elts
                                if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return handled


DISPATCHED = sorted(_dispatched_actions())


def test_the_walker_found_the_whole_surface():
    """Guard the guard: a parser that silently finds nothing would pass forever."""
    assert len(DISPATCHED) >= 21, f'expected all lager logic actions, got {DISPATCHED}'
    scripts = {s for s, _ in DISPATCHED}
    assert scripts == {'scope.py', 'enable_disable.py'}, scripts


@pytest.mark.parametrize('script,action', DISPATCHED, ids=lambda v: str(v))
def test_dispatched_action_is_handled_by_its_script(script, action):
    path = _impl_path(script)
    assert path.exists(), f'{script} is not in cli/impl/ (dispatching {action!r})'
    handled = _actions_handled(path)
    assert handled, f'no action names parsed out of {path.name}; the walker needs updating'
    assert action in handled, (
        f'`lager logic` sends {action!r} to {script}, which does not handle it. '
        f'{script} handles: {sorted(handled)}'
    )
