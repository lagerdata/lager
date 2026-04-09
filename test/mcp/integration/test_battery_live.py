# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Live integration tests for battery simulator MCP tools."""

import pytest

from lager.mcp.tools.battery import (
    lager_battery_soc,
    lager_battery_voc,
    lager_battery_enable,
    lager_battery_disable,
    lager_battery_state,
    lager_battery_current_limit,
    lager_battery_capacity,
    lager_battery_mode,
    lager_battery_set,
    lager_battery_ovp,
    lager_battery_ocp,
    lager_battery_batt_full,
    lager_battery_batt_empty,
    lager_battery_model,
    lager_battery_clear,
    lager_battery_clear_ovp,
    lager_battery_clear_ocp,
)

NET = "battery1"


@pytest.mark.integration
@pytest.mark.battery
class TestBatteryLive:

    @pytest.fixture(autouse=True)
    def safety_teardown(self, box1):
        """Always disable the battery simulator after each test."""
        yield
        lager_battery_disable(box=box1, net=NET)

    def test_read_soc(self, box1):
        """Reading state of charge should return output without errors."""
        result = lager_battery_soc(box=box1, net=NET)
        assert "Error" not in result

    def test_read_voc(self, box1):
        """Reading open circuit voltage should return output without errors."""
        result = lager_battery_voc(box=box1, net=NET)
        assert "Error" not in result

    def test_set_soc(self, box1):
        """Setting SOC to 50% should succeed."""
        result = lager_battery_soc(box=box1, net=NET, value=50.0)
        assert "Error" not in result

    def test_set_voc(self, box1):
        """Setting VOC to 3.7V should succeed."""
        result = lager_battery_voc(box=box1, net=NET, value=3.7)
        assert "Error" not in result

    def test_read_state(self, box1):
        """Reading battery state should return output without errors."""
        result = lager_battery_state(box=box1, net=NET)
        assert "Error" not in result

    def test_enable_disable(self, box1):
        """Enable, check state, then disable the battery simulator."""
        enable_result = lager_battery_enable(box=box1, net=NET)
        assert "Error" not in enable_result

        state_result = lager_battery_state(box=box1, net=NET)
        assert "Error" not in state_result

        disable_result = lager_battery_disable(box=box1, net=NET)
        assert "Error" not in disable_result

    def test_current_limit_read_write(self, box1):
        """Reading and writing current limit should succeed."""
        read_result = lager_battery_current_limit(box=box1, net=NET)
        assert "Error" not in read_result

        write_result = lager_battery_current_limit(box=box1, net=NET, value=1.0)
        assert "Error" not in write_result

    def test_capacity_read_write(self, box1):
        """Reading and writing capacity should succeed."""
        read_result = lager_battery_capacity(box=box1, net=NET)
        assert "Error" not in read_result

        write_result = lager_battery_capacity(box=box1, net=NET, amp_hours=2.0)
        assert "Error" not in write_result

    def test_mode_read_write(self, box1):
        """Reading and writing mode should succeed."""
        read_result = lager_battery_mode(box=box1, net=NET)
        assert "Error" not in read_result

        write_result = lager_battery_mode(box=box1, net=NET, mode_type="static")
        assert "Error" not in write_result

    def test_set_command(self, box1):
        """lager battery set should apply the configuration."""
        result = lager_battery_set(box=box1, net=NET)
        assert "Error" not in result

    def test_ovp_read_write(self, box1):
        """Reading and writing OVP threshold should succeed."""
        read_result = lager_battery_ovp(box=box1, net=NET)
        assert "Error" not in read_result

        write_result = lager_battery_ovp(box=box1, net=NET, voltage=4.5)
        assert "Error" not in write_result

    def test_ocp_read_write(self, box1):
        """Reading and writing OCP threshold should succeed."""
        read_result = lager_battery_ocp(box=box1, net=NET)
        assert "Error" not in read_result

        write_result = lager_battery_ocp(box=box1, net=NET, current=3.0)
        assert "Error" not in write_result

    def test_batt_full_read_write(self, box1):
        """Reading and writing batt-full voltage should succeed."""
        read_result = lager_battery_batt_full(box=box1, net=NET)
        assert "Error" not in read_result

        write_result = lager_battery_batt_full(box=box1, net=NET, voltage=4.2)
        assert "Error" not in write_result

    def test_batt_empty_read_write(self, box1):
        """Reading and writing batt-empty voltage should succeed."""
        read_result = lager_battery_batt_empty(box=box1, net=NET)
        assert "Error" not in read_result

        write_result = lager_battery_batt_empty(box=box1, net=NET, voltage=3.0)
        assert "Error" not in write_result

    def test_model_read_write(self, box1):
        """Reading and writing battery model should succeed."""
        read_result = lager_battery_model(box=box1, net=NET)
        assert "Error" not in read_result

        write_result = lager_battery_model(box=box1, net=NET, partnumber="18650")
        assert "Error" not in write_result

    def test_clear(self, box1):
        """Clearing all battery faults should succeed."""
        result = lager_battery_clear(box=box1, net=NET)
        assert "Error" not in result

    def test_clear_ovp(self, box1):
        """Clearing OVP fault should succeed."""
        result = lager_battery_clear_ovp(box=box1, net=NET)
        assert "Error" not in result

    def test_clear_ocp(self, box1):
        """Clearing OCP fault should succeed."""
        result = lager_battery_clear_ocp(box=box1, net=NET)
        assert "Error" not in result
