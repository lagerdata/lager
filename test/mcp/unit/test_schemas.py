# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP schema models (v0 scope)."""

import json
import pytest

from lager.mcp.schemas.bench import (
    BenchDefinition,
    CalibrationStatus,
    DUTSlot,
    InstrumentDescriptor,
    VoltageRange,
)
from lager.mcp.schemas.net import InterfaceDescriptor, NetDescriptor, SafetyLimits
from lager.mcp.schemas.capability import CapabilityGraph, CapabilityNode, CapabilityRole
from lager.mcp.schemas.safety_types import PreflightResult, RateLimit, SafetyConstraints
from lager.mcp.schemas.heuristic import (
    CapabilityMatch,
    Substitution,
    SuitabilityReport,
    TestRequirement,
)
from lager.mcp.schemas.scenario import (
    Assertion,
    Scenario,
    ScenarioResult,
    ScenarioStep,
    StepResult,
)


class TestBenchSchemas:
    def test_voltage_range(self):
        vr = VoltageRange(min_v=0, max_v=5.0, nominal_v=3.3)
        assert vr.min_v == 0
        assert vr.max_v == 5.0
        assert vr.nominal_v == 3.3

    def test_dut_slot(self):
        ds = DUTSlot(name="slot0", active=True, board_profile="nrf52840")
        assert ds.name == "slot0"
        assert ds.active is True

    def test_instrument_descriptor(self):
        inst = InstrumentDescriptor(
            name="psu",
            instrument_type="rigol_dp800",
            connection="TCPIP::192.168.1.100",
            channels=["CH1", "CH2"],
        )
        assert inst.instrument_type == "rigol_dp800"
        assert len(inst.channels) == 2

    def test_bench_definition_minimal(self):
        bench = BenchDefinition()
        assert bench.box_id == ""
        assert bench.nets == []

    def test_bench_definition_full(self):
        bench = BenchDefinition(
            box_id="HW-7",
            hostname="hw7",
            version="1.0.0",
            dut_slots=[DUTSlot(name="main")],
            nets=[
                NetDescriptor(name="psu1", net_type="power-supply", instrument="rigol_dp800", channel="1"),
            ],
        )
        assert bench.box_id == "HW-7"
        assert len(bench.nets) == 1
        assert bench.nets[0].name == "psu1"

    def test_bench_serialization_roundtrip(self):
        bench = BenchDefinition(box_id="HW-7", hostname="hw7")
        data = json.loads(bench.model_dump_json())
        bench2 = BenchDefinition(**data)
        assert bench2.box_id == "HW-7"


class TestNetSchemas:
    def test_net_descriptor_defaults(self):
        nd = NetDescriptor(name="gpio0", net_type="gpio")
        assert nd.controllable is True
        assert nd.observable is True
        assert nd.directionality == "bidirectional"
        assert nd.roles == []

    def test_safety_limits(self):
        sl = SafetyLimits(max_voltage=5.0, max_current=0.5)
        assert sl.max_voltage == 5.0

    def test_interface_descriptor(self):
        iface = InterfaceDescriptor(
            name="spi0",
            protocol="spi",
            nets=["mosi", "miso", "sck", "cs"],
            roles=["protocol_master"],
        )
        assert iface.protocol == "spi"
        assert len(iface.nets) == 4


class TestCapabilitySchemas:
    def test_capability_role_values(self):
        assert CapabilityRole.SOURCE_POWER.value == "source_power"
        assert CapabilityRole.PROTOCOL_MASTER.value == "protocol_master"
        assert CapabilityRole.FLASH_FIRMWARE.value == "flash_firmware"

    def test_capability_node(self):
        node = CapabilityNode(
            role=CapabilityRole.SOURCE_POWER,
            target="psu1",
            confidence=0.95,
        )
        assert node.role == CapabilityRole.SOURCE_POWER
        assert node.confidence == 0.95

    def test_capability_graph_queries(self):
        graph = CapabilityGraph(
            box_id="HW-7",
            nodes=[
                CapabilityNode(role=CapabilityRole.SOURCE_POWER, target="psu1"),
                CapabilityNode(role=CapabilityRole.SOURCE_POWER, target="batt1"),
                CapabilityNode(role=CapabilityRole.PROTOCOL_MASTER, target="spi0"),
                CapabilityNode(role=CapabilityRole.FLASH_FIRMWARE, target="debug1"),
            ],
        )
        assert graph.has_role(CapabilityRole.SOURCE_POWER)
        assert not graph.has_role(CapabilityRole.EMULATE_DEVICE)
        assert graph.targets_for_role(CapabilityRole.SOURCE_POWER) == ["psu1", "batt1"]
        assert graph.roles_for_target("psu1") == [CapabilityRole.SOURCE_POWER]
        assert len(graph.by_role(CapabilityRole.SOURCE_POWER)) == 2
        assert len(graph.by_target("debug1")) == 1


class TestSafetySchemas:
    def test_safety_constraints(self):
        sc = SafetyConstraints(
            max_voltage={"psu1": 5.0},
            max_current={"psu1": 1.0},
            dangerous_actions=["flash_firmware"],
        )
        assert sc.max_voltage["psu1"] == 5.0
        assert "flash_firmware" in sc.dangerous_actions
        assert sc.destructive_mode is False

    def test_preflight_result_allowed(self):
        pr = PreflightResult(allowed=True)
        assert pr.allowed
        assert pr.blocked_reason is None

    def test_preflight_result_blocked(self):
        pr = PreflightResult(
            allowed=False,
            blocked_reason="Voltage too high",
            mitigations=["Reduce voltage"],
        )
        assert not pr.allowed
        assert pr.blocked_reason == "Voltage too high"


class TestHeuristicSchemas:
    def test_test_requirement(self):
        tr = TestRequirement(
            test_type="qspi_flash_driver",
            required_capabilities=[CapabilityRole.FLASH_FIRMWARE, CapabilityRole.PROTOCOL_MASTER],
            required_protocols=["spi"],
        )
        assert len(tr.required_capabilities) == 2

    def test_suitability_report(self):
        report = SuitabilityReport(
            test_type="qspi_flash_driver",
            box_id="HW-7",
            can_run=True,
            confidence=0.92,
            explanation="All required capabilities matched.",
        )
        assert report.can_run
        assert report.confidence == 0.92


class TestScenarioSchemas:
    def test_scenario_step(self):
        step = ScenarioStep(action="set_voltage", target="psu1", params={"voltage": 3.3})
        assert step.action == "set_voltage"
        assert step.on_failure == "abort"

    def test_scenario_step_defaults(self):
        step = ScenarioStep(action="wait")
        assert step.target is None
        assert step.params == {}
        assert step.max_retries == 0

    def test_scenario_full(self):
        scenario = Scenario(
            name="test_flash",
            setup=[ScenarioStep(action="set_voltage", target="psu1", params={"voltage": 3.3})],
            steps=[ScenarioStep(action="spi_transfer", target="spi0", params={"tx": "0x9F"})],
            cleanup=[ScenarioStep(action="disable_supply", target="psu1")],
            assertions=[Assertion(name="check", expression="True")],
        )
        assert scenario.name == "test_flash"
        assert len(scenario.setup) == 1
        assert len(scenario.steps) == 1
        assert len(scenario.cleanup) == 1
        assert len(scenario.assertions) == 1

    def test_scenario_gpio_button_roundtrip(self):
        scenario = Scenario(
            name="gpio_button_press_release",
            steps=[
                ScenarioStep(action="gpio_set", target="button0", params={"level": "high"}),
                ScenarioStep(action="wait", params={"ms": 100}),
                ScenarioStep(action="gpio_read", target="led0", params={"label": "after_press"}),
                ScenarioStep(action="gpio_set", target="button0", params={"level": "low"}),
                ScenarioStep(action="wait", params={"ms": 100}),
                ScenarioStep(action="gpio_read", target="led0", params={"label": "after_release"}),
            ],
            assertions=[
                Assertion(name="press_detected", expression="results['after_press']['value'] == 1"),
                Assertion(name="release_detected", expression="results['after_release']['value'] == 0"),
            ],
        )
        payload = json.loads(scenario.model_dump_json())
        s2 = Scenario(**payload)
        assert s2.name == "gpio_button_press_release"
        assert len(s2.steps) == 6
        assert len(s2.assertions) == 2

    def test_scenario_result(self):
        result = ScenarioResult(scenario_name="test1", status="passed", duration_ms=1234.5)
        assert result.status == "passed"
        data = json.loads(result.model_dump_json())
        assert data["duration_ms"] == 1234.5

    def test_step_result(self):
        sr = StepResult(action="gpio_set", target="button0", success=True, duration_ms=1.5)
        assert sr.success
        assert sr.error is None
