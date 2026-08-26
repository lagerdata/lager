# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""`lager logic` must resolve nets under the type its own role maps to.

The CLI validates the net as role `logic` before dispatching
(cli/commands/measurement/logic.py), and the box-side worker then looks it up
with Net.get(name, <NetType>). Both of Net.get's lookup paths match on type
EQUALITY -- box/lager/nets/net.py:405 and :695 -- so if those two disagree,
Net.get returns None rather than raising, the `if target_net:` guard goes
false, and every `lager logic` subcommand exits 0 having done nothing.

That is exactly what shipped: the worker asked for NetType.Analog while the
command validated role `logic`. Confirmed on a box with a Rigol MSO logic net
configured -- `lager logic --box <BOX>` listed it, which is only possible if
its role is `logic`, and the enable was a no-op.

These tests pin the two sides together. They call the workers with a stub Net
so no instrument is needed, and compare against the REAL NetType.from_role, so
renaming the role or the enum member fails here rather than in the field.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")
if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)

from lager.nets.constants import NetType  # noqa: E402

# The role string the CLI validates against before dispatching to the worker.
from cli.commands.measurement.logic import LOGIC_ROLE  # noqa: E402

import cli.impl.power.enable_disable as worker  # noqa: E402

# Every worker in the module, and the action name that dispatches to it.
WORKERS = [
    "disable_net",
    "enable_net",
    "start_capture",
    "stop_capture",
    "start_single",
    "force_trigger",
]


@pytest.fixture
def recorded_net_gets(monkeypatch):
    """Capture the (name, type) every worker passes to Net.get."""
    calls = []

    class _StubNet:
        @staticmethod
        def get(name, net_type, **kwargs):
            calls.append((name, net_type))
            return None          # worker's `if target_net:` goes false; fine

    import lager.nets.net as real_net
    monkeypatch.setattr(real_net, "Net", _StubNet)
    return calls


def test_logic_role_maps_to_logic_net_type():
    """The premise the workers rely on."""
    assert NetType.from_role(LOGIC_ROLE) is NetType.Logic


@pytest.mark.parametrize("worker_name", WORKERS)
def test_worker_resolves_the_type_the_role_maps_to(worker_name, recorded_net_gets):
    getattr(worker, worker_name)("logic1")

    assert recorded_net_gets, f"{worker_name} did not call Net.get"
    name, net_type = recorded_net_gets[0]
    assert name == "logic1"
    assert net_type is NetType.from_role(LOGIC_ROLE), (
        f"{worker_name} looks the net up as {net_type}, but the CLI validates it "
        f"as role {LOGIC_ROLE!r} -> {NetType.from_role(LOGIC_ROLE)}. Net.get matches "
        f"on type equality, so this mismatch makes the command a silent no-op."
    )


def test_no_worker_uses_the_analog_type(recorded_net_gets):
    """The specific regression: Analog was used for all six."""
    for worker_name in WORKERS:
        recorded_net_gets.clear()
        getattr(worker, worker_name)("logic1")
        _, net_type = recorded_net_gets[0]
        assert net_type is not NetType.Analog, (
            f"{worker_name} resolves logic nets as NetType.Analog again"
        )


# ---------------------------------------------------------------------------
# The same defect, one layer over: cli/impl/measurement/scope.py
# ---------------------------------------------------------------------------
# #319 repointed `lager logic`'s sixteen measure/trigger/cursor actions at
# scope.py, which had been serving `lager scope` alone and hardcoded
# NetType.Analog in both of its net-resolution helpers. Unlike the workers
# above, Net.get RAISES InvalidNetError there rather than returning None, so
# these failed loudly with "Invalid Net: logic1" instead of silently -- but the
# cause is identical, and so is the fix: resolve under the type the dispatching
# command's role maps to.

import cli.impl.measurement.scope as scope_worker  # noqa: E402
from cli.commands.measurement.scope import SCOPE_ROLE  # noqa: E402


@pytest.fixture
def recorded_scope_net_gets(monkeypatch):
    """Capture the (name, type) scope.py passes to Net.get."""
    calls = []

    class _StubNet:
        @staticmethod
        def get(name, net_type, **kwargs):
            calls.append((name, net_type))
            return None

    import lager
    monkeypatch.setattr(lager, "Net", _StubNet, raising=False)
    monkeypatch.setattr(scope_worker, "_NET_ROLE", "scope")
    return calls


@pytest.mark.parametrize("role", [LOGIC_ROLE, SCOPE_ROLE])
def test_scope_worker_resolves_the_type_the_role_maps_to(
    role, recorded_scope_net_gets, monkeypatch
):
    monkeypatch.setattr(scope_worker, "_NET_ROLE", role)
    scope_worker.get_rigol_net("net1")

    assert recorded_scope_net_gets, "get_rigol_net did not call Net.get"
    name, net_type = recorded_scope_net_gets[0]
    assert name == "net1"
    assert net_type is NetType.from_role(role), (
        f"scope.py resolves a {role!r} net as {net_type}, but the CLI validates "
        f"it as role {role!r} -> {NetType.from_role(role)}. Net.get matches on "
        f"type equality and raises InvalidNetError when nothing matches."
    )


@pytest.mark.parametrize("role", [LOGIC_ROLE, SCOPE_ROLE])
def test_scope_source_net_resolves_the_same_way(
    role, recorded_scope_net_gets, monkeypatch
):
    """get_source_net is the second resolution path and was missed once already."""
    monkeypatch.setattr(scope_worker, "_NET_ROLE", role)
    scope_worker.get_source_net("net2")

    assert recorded_scope_net_gets, "get_source_net did not call Net.get"
    _, net_type = recorded_scope_net_gets[0]
    assert net_type is NetType.from_role(role)


def test_scope_worker_does_not_hardcode_analog_for_logic(
    recorded_scope_net_gets, monkeypatch
):
    """The specific regression: every net resolved as Analog, whatever it was."""
    monkeypatch.setattr(scope_worker, "_NET_ROLE", LOGIC_ROLE)
    for getter in (scope_worker.get_rigol_net, scope_worker.get_source_net):
        recorded_scope_net_gets.clear()
        getter("logic1")
        _, net_type = recorded_scope_net_gets[0]
        assert net_type is not NetType.Analog, (
            f"{getter.__name__} resolves logic nets as NetType.Analog again"
        )


def test_scope_worker_defaults_to_scope_when_the_role_is_absent():
    """A CLI predating the role key must keep working, unchanged."""
    assert scope_worker._NET_ROLE == "scope"
    assert NetType.from_role("scope") is NetType.Analog


def test_saved_net_lookup_follows_the_role_too(monkeypatch):
    """get_net_info filtered on role == "scope", so is_rigol()/is_picoscope()
    saw None for a logic net and the basic-op dispatchers reported it as "not
    found or not a scope net" -- a second, independent way the same net went
    unresolvable."""
    saved = [
        {"name": "logic1", "role": "logic", "instrument": "Rigol MSO5204"},
        {"name": "scope1", "role": "scope", "instrument": "Rigol MSO5204"},
    ]
    monkeypatch.setattr(scope_worker, "load_saved_nets", lambda: saved)

    monkeypatch.setattr(scope_worker, "_NET_ROLE", LOGIC_ROLE)
    info = scope_worker.get_net_info("logic1")
    assert info is not None, "a logic net is still invisible to get_net_info"
    assert scope_worker.is_rigol(info)

    monkeypatch.setattr(scope_worker, "_NET_ROLE", SCOPE_ROLE)
    assert scope_worker.get_net_info("scope1") is not None
    assert scope_worker.get_net_info("logic1") is None
