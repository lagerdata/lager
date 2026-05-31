# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lager.mcp.tools.box -- on-box box_manage MCP tool."""

import json
from unittest.mock import patch

import pytest

from lager.mcp.schemas.bench import BenchDefinition, InstrumentDescriptor
from lager.mcp.schemas.capability import CapabilityGraph, CapabilityNode, CapabilityRole
from lager.mcp.schemas.net import NetDescriptor


@pytest.mark.unit
class TestBoxTools:
    """Tests for box_manage (health / reload)."""

    @patch("lager.mcp.server_state.get_bench")
    @patch("lager.mcp.config.get_box_version")
    @patch("lager.mcp.config.get_box_id")
    def test_box_manage_health(self, mock_box_id, mock_version, mock_get_bench):
        mock_box_id.return_value = "demo-box"
        mock_version.return_value = "1.0.0"
        bench = BenchDefinition(
            nets=[NetDescriptor(name="uart0", net_type="uart")],
            instruments=[
                InstrumentDescriptor(
                    name="ps1",
                    instrument_type="rigol_dp800",
                    connection="USB::0x1234::INSTR",
                    channels=["1"],
                ),
            ],
        )
        mock_get_bench.return_value = bench
        from lager.mcp.tools.box import box_manage

        result = json.loads(box_manage("health"))
        assert result["status"] == "ok"
        assert result["box_id"] == "demo-box"
        assert result["version"] == "1.0.0"
        assert result["nets"] == 1
        assert result["instruments"] == 1

    @patch("lager.mcp.server_state.get_capability_graph")
    @patch("lager.mcp.server_state.get_bench")
    @patch("lager.mcp.server_state.reload_bench")
    def test_box_manage_reload(self, mock_reload, mock_get_bench, mock_get_graph):
        bench = BenchDefinition(
            nets=[NetDescriptor(name="n1", net_type="gpio")],
            instruments=[
                InstrumentDescriptor(name="lj", instrument_type="labjack_t7", connection="usb", channels=[]),
            ],
        )
        mock_get_bench.return_value = bench
        graph = CapabilityGraph(
            nodes=[
                CapabilityNode(role=CapabilityRole.MEASURE, target="n1"),
            ],
        )
        mock_get_graph.return_value = graph
        from lager.mcp.tools.box import box_manage

        result = json.loads(box_manage("reload"))
        mock_reload.assert_called_once()
        assert result["status"] == "ok"
        assert result["nets"] == 1
        assert result["instruments"] == 1
        assert result["capabilities"] == 1

    # -- server_state / config failure paths --------------------------------

    @patch("lager.mcp.server_state.get_bench")
    @patch("lager.mcp.config.get_box_version")
    @patch("lager.mcp.config.get_box_id")
    def test_box_manage_health_get_bench_failure(self, mock_box_id, mock_version, mock_get_bench):
        mock_box_id.return_value = "b"
        mock_version.return_value = "v"
        mock_get_bench.side_effect = RuntimeError("bench unavailable")
        from lager.mcp.tools.box import box_manage

        with pytest.raises(RuntimeError, match="bench unavailable"):
            box_manage("health")

    @patch("lager.mcp.server_state.get_capability_graph")
    @patch("lager.mcp.server_state.get_bench")
    @patch("lager.mcp.server_state.reload_bench")
    def test_box_manage_reload_failure(
        self, mock_reload, mock_get_bench, mock_get_graph,
    ):
        mock_reload.side_effect = RuntimeError("reload failed")
        mock_get_bench.return_value = BenchDefinition()
        mock_get_graph.return_value = CapabilityGraph()
        from lager.mcp.tools.box import box_manage

        with pytest.raises(RuntimeError, match="reload failed"):
            box_manage("reload")
