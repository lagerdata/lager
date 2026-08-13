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
