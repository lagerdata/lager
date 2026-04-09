# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the heuristic engine (trimmed v0 library)."""

import pytest

from lager.mcp.engine.bench_loader import load_from_dicts
from lager.mcp.engine.capability_graph import build_capability_graph
from lager.mcp.engine.heuristic_engine import (
    BUILTIN_REQUIREMENTS,
    assess_suitability,
    infer_requirements,
)
from lager.mcp.schemas.capability import CapabilityRole


def _make_graph(*net_specs):
    raw_nets = [
        {"name": n, "role": r, "instrument": i, "channel": "0"}
        for n, r, i in net_specs
    ]
    bench = load_from_dicts(raw_nets=raw_nets, hello_data={"box_id": "TEST"})
    return build_capability_graph(bench)


class TestBuiltinLibrary:
    def test_library_size(self):
        assert len(BUILTIN_REQUIREMENTS) == 6

    def test_all_builtin_requirements_valid(self):
        for req in BUILTIN_REQUIREMENTS:
            assert req.test_type != ""
            assert len(req.required_capabilities) > 0

    def test_expected_types(self):
        types = {r.test_type for r in BUILTIN_REQUIREMENTS}
        expected = {
            "gpio_validation",
            "gpio_button_validation",
            "firmware_flash_and_boot",
            "power_consumption",
            "spi_slave_validation",
            "uart_loopback",
        }
        assert types == expected


class TestInferRequirements:
    def test_gpio_keyword(self):
        req = infer_requirements("Actuate GPIO and check digital IO")
        assert req.test_type == "gpio_validation"

    def test_button_keyword(self):
        req = infer_requirements("Button press/release test")
        assert req.test_type == "gpio_button_validation"

    def test_gpio_button_validation_exact(self):
        req = infer_requirements("gpio_button_validation")
        assert req.test_type == "gpio_button_validation"

    def test_gpio_button_keyword_phrase(self):
        req = infer_requirements("Validate GPIO button behavior on DUT")
        assert req.test_type == "gpio_button_validation"

    def test_spi_keyword(self):
        req = infer_requirements("SPI slave device validation")
        assert req.test_type == "spi_slave_validation"

    def test_uart_keyword(self):
        req = infer_requirements("UART serial loopback test")
        assert req.test_type == "uart_loopback"

    def test_power_keyword(self):
        req = infer_requirements("Measure power consumption")
        assert req.test_type == "power_consumption"

    def test_firmware_keyword(self):
        req = infer_requirements("Flash firmware and verify boot")
        assert req.test_type == "firmware_flash_and_boot"

    def test_exact_key_match(self):
        req = infer_requirements("gpio_validation")
        assert req.test_type == "gpio_validation"

    def test_unknown_falls_back(self):
        req = infer_requirements("Quantum entanglement test")
        assert req.test_type == "custom"
        assert CapabilityRole.RUN_LOCAL_PROGRAM in req.required_capabilities


class TestAssessSuitability:
    def test_gpio_full_match(self):
        graph = _make_graph(
            ("psu1", "power-supply", "rigol_dp800"),
            ("gpio0", "gpio", "labjack_t7"),
        )
        req = infer_requirements("gpio_validation")
        report = assess_suitability(req, graph)
        assert report.can_run is True
        assert report.confidence > 0.5
        assert len(report.missing_required) == 0

    def test_gpio_missing_power(self):
        graph = _make_graph(
            ("gpio0", "gpio", "labjack_t7"),
        )
        req = infer_requirements("gpio_validation")
        report = assess_suitability(req, graph)
        assert report.can_run is False
        assert CapabilityRole.SOURCE_POWER in report.missing_required

    def test_gpio_button_validation_full_bench(self):
        """Bench with GPIO + UART satisfies all required capabilities."""
        graph = _make_graph(
            ("button0", "gpio", "labjack_t7"),
            ("led0", "gpio", "labjack_t7"),
            ("uart0", "uart", "ftdi"),
        )
        req = infer_requirements("gpio_button_validation")
        report = assess_suitability(req, graph)
        assert report.can_run is True
        assert "drive" in report.candidate_nets
        assert "observe" in report.candidate_nets
        assert "protocol_master" in report.candidate_nets

    def test_gpio_button_validation_gpio_only_missing_uart(self):
        """GPIO only -- missing PROTOCOL_MASTER from UART."""
        graph = _make_graph(
            ("button0", "gpio", "labjack_t7"),
            ("led0", "gpio", "labjack_t7"),
        )
        req = infer_requirements("gpio_button_validation")
        report = assess_suitability(req, graph)
        assert report.can_run is False
        assert CapabilityRole.PROTOCOL_MASTER in report.missing_required

    def test_gpio_button_validation_no_gpio(self):
        graph = _make_graph(
            ("psu1", "power-supply", "rigol_dp800"),
        )
        req = infer_requirements("gpio_button_validation")
        report = assess_suitability(req, graph)
        assert report.can_run is False

    def test_spi_match(self):
        graph = _make_graph(
            ("psu1", "power-supply", "rigol_dp800"),
            ("spi0", "spi", "aardvark"),
            ("debug1", "debug", "jlink"),
        )
        req = infer_requirements("SPI slave validation")
        report = assess_suitability(req, graph)
        assert report.can_run is True

    def test_candidate_nets(self):
        graph = _make_graph(
            ("psu1", "power-supply", "rigol_dp800"),
            ("psu2", "power-supply", "keysight_e36000"),
        )
        req = infer_requirements("power consumption measurement")
        report = assess_suitability(req, graph)
        assert "source_power" in report.candidate_nets
        assert len(report.candidate_nets["source_power"]) >= 1

    def test_box_id_in_report(self):
        graph = _make_graph(("psu1", "power-supply", "rigol_dp800"))
        graph.box_id = "HW-42"
        req = infer_requirements("power consumption")
        report = assess_suitability(req, graph)
        assert report.box_id == "HW-42"

    def test_substitution(self):
        graph = _make_graph(
            ("psu1", "power-supply", "rigol_dp800"),
            ("spi0", "spi", "aardvark"),
            ("debug1", "debug", "jlink"),
            ("scope1", "analog", "rigol_mso5000"),
        )
        req = infer_requirements("SPI slave validation")
        report = assess_suitability(req, graph)
        assert report.can_run is True
