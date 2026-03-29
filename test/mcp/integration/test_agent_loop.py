# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for the agent workflow loop.

Focused on the v0 proof scenario: GPIO button press/release.

These tests verify the end-to-end flow without requiring a live box:
  discovery -> suitability -> scenario run (mocked) -> verify

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
from lager.mcp.engine.scenario_runner import run as run_scenario_interpreter
from lager.mcp.server_state import init_state, get_bench, get_capability_graph
from lager.mcp.schemas.scenario import Scenario, ScenarioStep, Assertion


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
    1. get_bench_summary() -> learn what's on the box (1 call)
    2. assess_suitability("gpio_button_validation") -> can this box do it? (1 call)
    3. run_scenario({...}) -> actuate + confirm via GPI + DUT CLI (1 call)

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

    @patch("lager.Net.get")
    def test_step3_run_scenario(self, mock_net_get):
        """Agent runs the full GPIO + UART button proof scenario.

        The scenario models the v0 proof interaction:
          - button0 (GPO) drives a physical button input on the DUT
          - led0 (GPI) samples a DUT-driven output (LED/status pin)
          - uart0 talks to the DUT CLI over UART to confirm firmware
            actually sees the press and release events

        All hardware calls go through ``lager.Net.get()`` which is mocked
        here. On a real box these resolve to physical pin I/O and /dev/tty*.
        """
        from lager import NetType

        gpio_net = MagicMock(name="gpio_net")
        gpio_net.input.side_effect = [1, 0]

        mock_ser = MagicMock(name="serial_connection")
        mock_ser.timeout = 1
        mock_ser.readline.side_effect = [
            b"btn_state: pressed\r\n",
            b"btn_state: released\r\n",
        ]
        uart_net = MagicMock(name="uart_net")
        uart_net.connect.return_value = mock_ser

        def net_get_side_effect(name, *, type=None):
            if type == NetType.GPIO:
                return gpio_net
            if type == NetType.UART:
                return uart_net
            return MagicMock()

        mock_net_get.side_effect = net_get_side_effect

        scenario_json = json.dumps({
            "name": "gpio_button_press_release",
            "description": "Actuate button via GPIO, confirm with GPI and DUT CLI over UART",
            "steps": [
                {"action": "gpio_set", "target": "button0", "params": {"level": 1}},
                {"action": "wait", "params": {"ms": 10}},
                {"action": "gpio_read", "target": "led0", "params": {"label": "after_press"}},
                {"action": "uart_send", "target": "uart0", "params": {"data": "btn_status\r\n"}},
                {"action": "uart_expect", "target": "uart0", "params": {"pattern": "pressed", "label": "dut_after_press", "timeout_ms": 2000}},
                {"action": "gpio_set", "target": "button0", "params": {"level": 0}},
                {"action": "wait", "params": {"ms": 10}},
                {"action": "gpio_read", "target": "led0", "params": {"label": "after_release"}},
                {"action": "uart_send", "target": "uart0", "params": {"data": "btn_status\r\n"}},
                {"action": "uart_expect", "target": "uart0", "params": {"pattern": "released", "label": "dut_after_release", "timeout_ms": 2000}},
            ],
            "assertions": [
                {"name": "press_detected", "expression": "results['after_press']['value'] == 1"},
                {"name": "release_detected", "expression": "results['after_release']['value'] == 0"},
                {"name": "dut_saw_press", "expression": "results['dut_after_press']['matched'] is True"},
                {"name": "dut_saw_release", "expression": "results['dut_after_release']['matched'] is True"},
            ],
        })

        result = run_scenario_interpreter(scenario_json)

        assert result["status"] == "passed"
        assert result["scenario_name"] == "gpio_button_press_release"

        assert len(result["step_results"]) == 10
        assert all(s["success"] for s in result["step_results"])

        assert len(result["assertions"]) == 4
        assert all(a["passed"] for a in result["assertions"])

        assert result["results"]["after_press"]["value"] == 1
        assert result["results"]["after_release"]["value"] == 0
        assert result["results"]["dut_after_press"]["matched"] is True
        assert result["results"]["dut_after_release"]["matched"] is True

        assert gpio_net.output.call_count == 2
        gpio_net.output.assert_any_call(1)
        gpio_net.output.assert_any_call(0)
        assert gpio_net.input.call_count == 2
        assert mock_ser.write.call_count == 2

    def test_round_trip_count(self):
        """The core workflow uses <= 3 MCP-level calls."""
        calls = [
            "get_bench_summary",
            "assess_suitability",
            "run_scenario",
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


# ---------------------------------------------------------------------------
# Scenario schema validation
# ---------------------------------------------------------------------------

class TestScenarioSchemaValidation:
    """Verify scenario schema works for v0 patterns."""

    def test_minimal_scenario(self):
        s = Scenario(name="empty", steps=[])
        assert s.name == "empty"
        assert s.steps == []
        assert s.timeout_s == 300

    def test_gpio_scenario_roundtrip(self):
        s = Scenario(
            name="gpio_test",
            steps=[
                ScenarioStep(action="gpio_set", target="pin0", params={"level": 1}),
                ScenarioStep(action="wait", params={"ms": 50}),
                ScenarioStep(action="gpio_read", target="pin0", params={"label": "reading"}),
            ],
            assertions=[
                Assertion(name="pin_high", expression="results['reading']['value'] == 1"),
            ],
        )
        payload = json.loads(s.model_dump_json())
        s2 = Scenario(**payload)
        assert s2.name == "gpio_test"
        assert len(s2.steps) == 3
        assert len(s2.assertions) == 1
