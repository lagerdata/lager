# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the capability graph builder."""

import pytest

from lager.mcp.engine.bench_loader import load_from_dicts
from lager.mcp.engine.capability_graph import build_capability_graph
from lager.mcp.schemas.capability import CapabilityRole


def _make_bench(*net_specs):
    raw_nets = []
    for name, role, instrument in net_specs:
        raw_nets.append({"name": name, "role": role, "instrument": instrument, "channel": "0"})
    return load_from_dicts(raw_nets=raw_nets, hello_data={"box_id": "TEST"})


class TestCapabilityGraphBuilder:
    def test_empty_bench(self):
        bench = load_from_dicts()
        graph = build_capability_graph(bench)
        # Always has RUN_LOCAL_PROGRAM
        assert graph.has_role(CapabilityRole.RUN_LOCAL_PROGRAM)
        assert len(graph.nodes) == 1

    def test_power_supply_roles(self):
        bench = _make_bench(("psu1", "power-supply", "rigol_dp800"))
        graph = build_capability_graph(bench)
        roles = graph.roles_for_target("psu1")
        assert CapabilityRole.SOURCE_POWER in roles
        assert CapabilityRole.DRIVE in roles
        assert CapabilityRole.MEASURE in roles
        assert CapabilityRole.SWEEP_VOLTAGE in roles

    def test_battery_roles(self):
        bench = _make_bench(("batt1", "battery", "keithley"))
        graph = build_capability_graph(bench)
        roles = graph.roles_for_target("batt1")
        assert CapabilityRole.SOURCE_POWER in roles
        assert CapabilityRole.SWEEP_VOLTAGE in roles

    def test_spi_roles(self):
        bench = _make_bench(("spi0", "spi", "aardvark"))
        graph = build_capability_graph(bench)
        roles = graph.roles_for_target("spi0")
        assert CapabilityRole.PROTOCOL_MASTER in roles
        assert CapabilityRole.CAPTURE_PROTOCOL in roles

    def test_i2c_roles(self):
        bench = _make_bench(("i2c0", "i2c", "aardvark"))
        graph = build_capability_graph(bench)
        assert CapabilityRole.PROTOCOL_CONTROLLER in graph.roles_for_target("i2c0")

    def test_debug_roles(self):
        bench = _make_bench(("debug1", "debug", "jlink"))
        graph = build_capability_graph(bench)
        roles = graph.roles_for_target("debug1")
        assert CapabilityRole.FLASH_FIRMWARE in roles
        assert CapabilityRole.CONTROL_STATE in roles

    def test_gpio_roles(self):
        bench = _make_bench(("gpio0", "gpio", "labjack_t7"))
        graph = build_capability_graph(bench)
        roles = graph.roles_for_target("gpio0")
        assert CapabilityRole.DRIVE in roles
        assert CapabilityRole.OBSERVE in roles
        assert CapabilityRole.CONTROL_STATE in roles

    def test_dac_roles(self):
        bench = _make_bench(("dac0", "dac", "labjack_t7"))
        graph = build_capability_graph(bench)
        roles = graph.roles_for_target("dac0")
        assert CapabilityRole.DRIVE in roles
        assert CapabilityRole.SWEEP_ANALOG in roles

    def test_scope_roles(self):
        bench = _make_bench(("scope1", "analog", "rigol_mso5000"))
        graph = build_capability_graph(bench)
        roles = graph.roles_for_target("scope1")
        assert CapabilityRole.OBSERVE in roles
        assert CapabilityRole.CAPTURE_WAVEFORM in roles

    def test_logic_roles(self):
        bench = _make_bench(("logic1", "logic", "rigol_mso5000"))
        graph = build_capability_graph(bench)
        assert CapabilityRole.CAPTURE_LOGIC in graph.roles_for_target("logic1")

    def test_uart_roles(self):
        bench = _make_bench(("uart0", "uart", "ftdi"))
        graph = build_capability_graph(bench)
        roles = graph.roles_for_target("uart0")
        assert CapabilityRole.OBSERVE in roles
        assert CapabilityRole.PROTOCOL_MASTER in roles

    def test_multi_net_bench(self):
        bench = _make_bench(
            ("psu1", "power-supply", "rigol_dp800"),
            ("spi0", "spi", "aardvark"),
            ("debug1", "debug", "jlink"),
            ("uart0", "uart", "ftdi"),
            ("gpio0", "gpio", "labjack_t7"),
        )
        graph = build_capability_graph(bench)
        assert graph.has_role(CapabilityRole.SOURCE_POWER)
        assert graph.has_role(CapabilityRole.PROTOCOL_MASTER)
        assert graph.has_role(CapabilityRole.FLASH_FIRMWARE)
        assert graph.has_role(CapabilityRole.OBSERVE)
        assert graph.has_role(CapabilityRole.CONTROL_STATE)
        assert graph.has_role(CapabilityRole.RUN_LOCAL_PROGRAM)
        # No roles for non-existent types
        assert not graph.has_role(CapabilityRole.EMULATE_DEVICE)

    def test_confidence_scores(self):
        bench = _make_bench(("wifi0", "wifi", "esp32"))
        graph = build_capability_graph(bench)
        nodes = graph.by_target("wifi0")
        # WiFi observe should have lower confidence
        assert all(n.confidence < 1.0 for n in nodes)

    def test_box_id_propagated(self):
        bench = load_from_dicts(hello_data={"box_id": "HW-42"})
        graph = build_capability_graph(bench)
        assert graph.box_id == "HW-42"
