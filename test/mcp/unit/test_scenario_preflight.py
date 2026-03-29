# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for scenario preflight validation wired into run_scenario."""

import json

import pytest

from lager.mcp.engine.bench_loader import load_from_dicts
from lager.mcp.engine.capability_graph import build_capability_graph
from lager.mcp.schemas.safety_types import SafetyConstraints
from lager.mcp.server_state import init_state
from lager.mcp.tools.scenario import _preflight_scenario


@pytest.fixture
def gpio_bench_state():
    """Initialize server state with a bench that has button0 and led0."""
    bench = load_from_dicts(
        raw_nets=[
            {"name": "psu1", "role": "power-supply", "instrument": "rigol_dp800", "channel": "1"},
            {"name": "button0", "role": "gpio", "instrument": "labjack_t7", "channel": "0"},
            {"name": "led0", "role": "gpio", "instrument": "labjack_t7", "channel": "1"},
        ],
        hello_data={"box_id": "HW-7"},
        bench_cfg={"constraints": {"max_voltage": {"psu1": 5.0}, "max_current": {"psu1": 1.0}}},
    )
    graph = build_capability_graph(bench)
    init_state(bench=bench, graph=graph)
    yield
    init_state()


class TestPreflightNetValidation:
    def test_valid_targets_pass(self, gpio_bench_state):
        steps = [
            {"action": "gpio_set", "target": "button0", "params": {"level": "high"}},
            {"action": "wait", "params": {"ms": 100}},
            {"action": "gpio_read", "target": "led0", "params": {"label": "reading"}},
        ]
        assert _preflight_scenario(steps) is None

    def test_unknown_net_rejected(self, gpio_bench_state):
        steps = [
            {"action": "gpio_set", "target": "nonexistent_pin", "params": {"level": "high"}},
        ]
        err = _preflight_scenario(steps)
        assert err is not None
        assert "nonexistent_pin" in err["error"]
        assert "does not exist" in err["error"]

    def test_no_target_steps_pass(self, gpio_bench_state):
        steps = [
            {"action": "wait", "params": {"ms": 50}},
        ]
        assert _preflight_scenario(steps) is None


class TestPreflightSafetyLimits:
    def test_voltage_within_limit_passes(self, gpio_bench_state):
        steps = [
            {"action": "set_voltage", "target": "psu1", "params": {"voltage": 3.3}},
        ]
        assert _preflight_scenario(steps) is None

    def test_voltage_exceeds_limit_blocked(self, gpio_bench_state):
        steps = [
            {"action": "set_voltage", "target": "psu1", "params": {"voltage": 12.0}},
        ]
        err = _preflight_scenario(steps)
        assert err is not None
        assert "Safety preflight blocked" in err["error"]
        assert "mitigations" in err

    def test_current_exceeds_limit_blocked(self, gpio_bench_state):
        steps = [
            {"action": "set_current", "target": "psu1", "params": {"current": 5.0}},
        ]
        err = _preflight_scenario(steps)
        assert err is not None
        assert "Safety preflight blocked" in err["error"]


class TestPreflightEmptyBench:
    def test_empty_bench_passes_no_targets(self):
        """With an empty bench (no nets), steps without targets pass."""
        init_state()
        steps = [{"action": "wait", "params": {"ms": 10}}]
        assert _preflight_scenario(steps) is None
        init_state()
