# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Live integration tests for power supply MCP tools."""

import pytest

from lager.mcp.tools.power import (
    lager_supply_voltage,
    lager_supply_current,
    lager_supply_state,
    lager_supply_enable,
    lager_supply_disable,
    lager_supply_set,
)

NET = "supply1"


@pytest.mark.integration
@pytest.mark.power
class TestPowerLive:

    @pytest.fixture(autouse=True)
    def safety_teardown(self, box1):
        """Always disable the supply after each test for hardware safety."""
        yield
        lager_supply_disable(box=box1, net=NET)

    def test_read_voltage(self, box1):
        """Reading voltage should return output without errors."""
        result = lager_supply_voltage(box=box1, net=NET)
        assert "Error" not in result

    def test_read_current(self, box1):
        """Reading current should return output without errors."""
        result = lager_supply_current(box=box1, net=NET)
        assert "Error" not in result

    def test_read_state(self, box1):
        """Reading state should return output without errors."""
        result = lager_supply_state(box=box1, net=NET)
        assert "Error" not in result

    def test_set_voltage(self, box1):
        """Setting voltage to 1.0V should succeed."""
        result = lager_supply_voltage(box=box1, net=NET, voltage=1.0)
        assert "Error" not in result

    def test_set_current(self, box1):
        """Setting current limit to 0.1A should succeed."""
        result = lager_supply_current(box=box1, net=NET, current=0.1)
        assert "Error" not in result

    def test_enable_disable(self, box1):
        """Enable, check state, then disable the supply."""
        enable_result = lager_supply_enable(box=box1, net=NET)
        assert "Error" not in enable_result

        state_result = lager_supply_state(box=box1, net=NET)
        assert "Error" not in state_result

        disable_result = lager_supply_disable(box=box1, net=NET)
        assert "Error" not in disable_result

    def test_set(self, box1):
        """lager supply set should apply the configuration."""
        result = lager_supply_set(box=box1, net=NET)
        assert "Error" not in result
