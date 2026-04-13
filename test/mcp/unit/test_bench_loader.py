# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the bench loader."""

import json
import os
import tempfile

import pytest

from lager.mcp.engine.bench_loader import load_from_dicts, load_from_files, _net_from_raw


class TestNetFromRaw:
    def test_power_supply(self):
        nd = _net_from_raw({"name": "psu1", "role": "power-supply", "instrument": "rigol_dp800", "channel": "1"})
        assert nd.name == "psu1"
        assert nd.net_type == "power-supply"
        assert nd.electrical_type == "power"
        assert "source_power" in nd.roles
        assert nd.controllable is True

    def test_spi(self):
        nd = _net_from_raw({"name": "spi0", "role": "spi", "instrument": "aardvark", "channel": "0"})
        assert nd.net_type == "spi"
        assert nd.electrical_type == "protocol"
        assert "protocol_master" in nd.roles

    def test_adc(self):
        nd = _net_from_raw({"name": "adc0", "role": "adc", "instrument": "labjack_t7", "channel": "0"})
        assert nd.net_type == "adc"
        assert nd.directionality == "input"
        assert nd.controllable is False

    def test_debug(self):
        nd = _net_from_raw({"name": "debug1", "role": "debug", "instrument": "jlink", "channel": "0"})
        assert "flash_firmware" in nd.roles
        assert "control_state" in nd.roles

    def test_unknown_role(self):
        nd = _net_from_raw({"name": "x", "role": "nonexistent", "instrument": "foo", "channel": "0"})
        assert nd.net_type == "nonexistent"
        assert nd.electrical_type == "unknown"
        assert nd.roles == []


class TestLoadFromDicts:
    def test_empty(self):
        bench = load_from_dicts()
        assert bench.box_id == ""
        assert bench.nets == []

    def test_with_nets(self):
        bench = load_from_dicts(
            raw_nets=[
                {"name": "psu1", "role": "power-supply", "instrument": "rigol_dp800", "channel": "1"},
                {"name": "spi0", "role": "spi", "instrument": "aardvark", "channel": "0"},
            ],
            hello_data={"box_id": "HW-7", "hostname": "hw7"},
        )
        assert bench.box_id == "HW-7"
        assert len(bench.nets) == 2
        assert bench.nets[0].name == "psu1"

    def test_interfaces_inferred(self):
        bench = load_from_dicts(
            raw_nets=[
                {"name": "spi0", "role": "spi", "instrument": "aardvark", "channel": "0"},
                {"name": "i2c0", "role": "i2c", "instrument": "aardvark", "channel": "0"},
                {"name": "psu1", "role": "power-supply", "instrument": "rigol_dp800", "channel": "1"},
            ],
        )
        assert len(bench.interfaces) == 2
        iface_names = {i.name for i in bench.interfaces}
        assert "spi0" in iface_names
        assert "i2c0" in iface_names

    def test_bench_cfg_overrides(self):
        bench = load_from_dicts(
            raw_nets=[
                {"name": "psu1", "role": "power-supply", "instrument": "rigol_dp800", "channel": "1"},
            ],
            bench_cfg={
                "box_id": "HW-42",
                "net_overrides": [
                    {"name": "psu1", "aliases": ["main_power", "vcc"], "voltage_domain": {"min_v": 0, "max_v": 32}},
                ],
            },
        )
        assert bench.box_id == "HW-42"
        assert bench.nets[0].aliases == ["main_power", "vcc"]
        assert bench.nets[0].voltage_domain.max_v == 32

    def test_safety_constraints(self):
        bench = load_from_dicts(
            bench_cfg={
                "constraints": {
                    "max_voltage": {"psu1": 5.0},
                    "max_current": {"psu1": 1.0},
                    "dangerous_actions": ["flash_firmware"],
                },
            },
        )
        assert bench.constraints is not None
        assert bench.constraints.max_voltage["psu1"] == 5.0

    def test_dut_slots(self):
        bench = load_from_dicts(
            bench_cfg={
                "dut_slots": [
                    {"name": "main", "active": True, "board_profile": "nrf52840"},
                ],
            },
        )
        assert len(bench.dut_slots) == 1
        assert bench.dut_slots[0].name == "main"


class TestLoadFromFiles:
    def test_missing_files(self):
        bench = load_from_files(
            saved_nets_path="/nonexistent/saved_nets.json",
            bench_json_path="/nonexistent/bench.json",
            box_id_path="/nonexistent/box_id",
        )
        assert bench.box_id == ""
        assert bench.nets == []

    def test_real_files(self, tmp_path):
        nets_file = tmp_path / "saved_nets.json"
        nets_file.write_text(json.dumps([
            {"name": "psu1", "role": "power-supply", "instrument": "rigol_dp800", "channel": "1"},
        ]))

        bench_file = tmp_path / "bench.json"
        bench_file.write_text(json.dumps({"box_id": "HW-99"}))

        box_id_file = tmp_path / "box_id"
        box_id_file.write_text("HW-99")

        bench = load_from_files(
            saved_nets_path=str(nets_file),
            bench_json_path=str(bench_file),
            box_id_path=str(box_id_file),
        )
        assert bench.box_id == "HW-99"
        assert len(bench.nets) == 1


class TestNullTolerance:
    """Regression: explicit JSON null values must not crash the loader.

    JSON allows a key to exist with value null, which is distinct from the key
    being absent. `dict.get(key, default)` only returns `default` for the
    absent case, so user data like `{"test_hints": null}` used to return None
    and crash downstream iteration.
    """

    def test_net_with_null_list_fields(self):
        nd = _net_from_raw({
            "name": "psu1",
            "role": "power-supply",
            "instrument": "rigol_dp800",
            "channel": "1",
            "aliases": None,
            "test_hints": None,
            "tags": None,
            "params": None,
            "description": None,
            "dut_connection": None,
        })
        assert nd.aliases == []
        assert nd.test_hints == []
        assert nd.tags == []
        assert nd.params == {}
        assert nd.description == ""
        assert nd.dut_connection == ""

    def test_bench_cfg_with_null_collections(self):
        bench = load_from_dicts(
            raw_nets=[{"name": "psu1", "role": "power-supply"}],
            bench_cfg={
                "net_overrides": None,
                "dut_slots": None,
                "interfaces": None,
            },
        )
        assert len(bench.nets) == 1
        assert bench.dut_slots == []

    def test_instrument_with_null_channels(self):
        bench = load_from_dicts(
            raw_nets=[],
            raw_instruments=[{"name": "scope1", "type": "rigol_mso5204", "channels": None}],
        )
        assert len(bench.instruments) == 1
        assert bench.instruments[0].channels == []
