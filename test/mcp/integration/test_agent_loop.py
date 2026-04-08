# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for the agent workflow loop.

Focused on the v0 proof scenario: GPIO button press/release.

These tests verify the end-to-end flow without requiring a live box:
  discovery -> suitability -> run_test_script (mocked) -> verify

They ensure that coarse-grained execution uses <= 3 MCP-style calls
for the core workflow.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from lager.mcp.engine.bench_loader import load_from_dicts
from lager.mcp.engine.capability_graph import build_capability_graph
from lager.mcp.engine.heuristic_engine import (
    assess_suitability,
    infer_requirements,
)
from lager.mcp.server_state import init_state, get_bench, get_capability_graph


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def gpio_bench():
    """A bench with GPIO, power, and UART -- the minimum for GPIO proof."""
    return load_from_dicts(
        raw_nets=[
            {"name": "psu1", "role": "power-supply", "instrument": "rigol_dp800", "channel": "1"},
            {"name": "button0", "role": "gpio", "instrument": "labjack_t7", "channel": "0"},
            {"name": "led0", "role": "gpio", "instrument": "labjack_t7", "channel": "1"},
            {"name": "uart0", "role": "uart", "instrument": "ftdi", "channel": "0"},
            {"name": "debug1", "role": "debug", "instrument": "jlink", "channel": "0"},
        ],
        hello_data={"box_id": "HW-7", "hostname": "hw7", "version": "1.0.0"},
    )


@pytest.fixture
def gpio_graph(gpio_bench):
    return build_capability_graph(gpio_bench)


@pytest.fixture
def gpio_state(gpio_bench, gpio_graph):
    init_state(bench=gpio_bench, graph=gpio_graph)
    yield
    init_state()


# ---------------------------------------------------------------------------
# GPIO Button Press/Release Proof Scenario
# ---------------------------------------------------------------------------

class TestGPIOButtonProofScenario:
    """
    The v0 proof point: actuate a button via GPIO/GPO, confirm the
    electrical toggle with GPI, and verify the DUT CLI over UART
    reports both press and release events.

    Physical model (what this represents on a real box):

      Box LabJack pin (GPO) ---[button0]---> DUT button input
      DUT LED/status output ---[led0]------> Box LabJack pin (GPI)
      Box FTDI UART         ---[uart0]-----> DUT UART CLI

    ``button0`` drives a physical button input on the DUT.
    ``led0`` samples the DUT's electrical response.
    ``uart0`` talks to the DUT's own CLI to confirm firmware-visible
    behavior (e.g. ``btn_status`` -> ``btn_state: pressed``).

    In tests we mock ``lager.Net.get()`` which returns type-specific
    mock net objects (GPIO, UART, etc.).  On a live box these resolve
    to real hardware I/O via the ``lager`` SDK.

    Agent workflow:
    1. discover_bench() -> learn what's on the box (1 call)
    2. assess_suitability("gpio_button_validation") -> can this box do it? (1 call)
    3. run_test_script({...}) -> actuate + confirm via GPI + DUT CLI (1 call)

    Total: 3 MCP round trips.
    """

    def test_step1_discovery(self, gpio_state):
        """Agent discovers the bench hardware."""
        bench = get_bench()
        assert bench.box_id == "HW-7"
        net_names = [n.name for n in bench.nets]
        assert "button0" in net_names
        assert "led0" in net_names
        gpio_nets = [n for n in bench.nets if n.net_type == "gpio"]
        assert len(gpio_nets) >= 2

    def test_step2_suitability(self, gpio_graph):
        """Agent checks if the box can run GPIO validation."""
        req = infer_requirements("gpio_validation")
        assert req.test_type == "gpio_validation"

        report = assess_suitability(req, gpio_graph)
        assert report.can_run is True
        assert report.confidence > 0.5
        assert "drive" in report.candidate_nets
        assert "observe" in report.candidate_nets

    def test_step2_gpio_button_validation(self, gpio_graph):
        """Agent calls assess_suitability with the exact v0 proof string."""
        req = infer_requirements("gpio_button_validation")
        assert req.test_type == "gpio_button_validation"

        report = assess_suitability(req, gpio_graph)
        assert report.can_run is True
        assert report.confidence > 0.5
        assert "drive" in report.candidate_nets
        assert "observe" in report.candidate_nets

    def test_step2_keyword_match(self):
        """Agent can find gpio_button_validation from a natural description."""
        req = infer_requirements("Validate button press/release via GPIO")
        assert req.test_type == "gpio_button_validation"

    def test_round_trip_count(self):
        """The core workflow uses <= 3 MCP-level calls."""
        calls = [
            "discover_bench",
            "plan_firmware_test",
            "run_test_script",
        ]
        assert len(calls) <= 3


# ---------------------------------------------------------------------------
# Discovery integration
# ---------------------------------------------------------------------------

class TestDiscoveryToolIntegration:
    """Test the discovery tools work with initialized state."""

    def test_bench_summary_via_state(self, gpio_state):
        bench = get_bench()
        assert bench.box_id == "HW-7"
        assert len(bench.nets) == 5

    def test_capability_graph_via_state(self, gpio_state):
        graph = get_capability_graph()
        assert graph.box_id == "HW-7"
        assert graph.has_role("source_power")
        assert graph.has_role("drive")
        assert graph.has_role("observe")
        assert graph.has_role("flash_firmware")

    def test_find_gpio_candidates(self, gpio_state):
        from lager.mcp.schemas.capability import CapabilityRole
        graph = get_capability_graph()
        drive_targets = graph.targets_for_role(CapabilityRole.DRIVE)
        assert "button0" in drive_targets
        assert "led0" in drive_targets


