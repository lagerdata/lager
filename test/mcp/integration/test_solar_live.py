# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Live integration tests for solar simulator MCP tools."""

import pytest

from lager.mcp.tools.solar import (
    lager_solar_set,
    lager_solar_stop,
    lager_solar_irradiance,
    lager_solar_mpp_current,
    lager_solar_mpp_voltage,
    lager_solar_resistance,
    lager_solar_temperature,
    lager_solar_voc,
)

NET = "solar1"


@pytest.mark.integration
@pytest.mark.solar
class TestSolarLive:

    @pytest.fixture(autouse=True)
    def safety_teardown(self, box1):
        """Always stop solar simulation after each test."""
        yield
        lager_solar_stop(box=box1, net=NET)

    def test_set(self, box1):
        """Applying solar simulator configuration should succeed."""
        result = lager_solar_set(box=box1, net=NET)
        assert "Error" not in result

    def test_stop(self, box1):
        """Stopping the solar simulator should succeed."""
        result = lager_solar_stop(box=box1, net=NET)
        assert "Error" not in result

    def test_read_irradiance(self, box1):
        """Reading irradiance should return output without errors."""
        result = lager_solar_irradiance(box=box1, net=NET)
        assert "Error" not in result

    def test_write_irradiance(self, box1):
        """Setting irradiance to 500.0 should succeed."""
        result = lager_solar_irradiance(box=box1, net=NET, value=500.0)
        assert "Error" not in result

    def test_mpp_current(self, box1):
        """Reading maximum power point current should succeed."""
        result = lager_solar_mpp_current(box=box1, net=NET)
        assert "Error" not in result

    def test_mpp_voltage(self, box1):
        """Reading maximum power point voltage should succeed."""
        result = lager_solar_mpp_voltage(box=box1, net=NET)
        assert "Error" not in result

    def test_read_resistance(self, box1):
        """Reading series resistance should return output without errors."""
        result = lager_solar_resistance(box=box1, net=NET)
        assert "Error" not in result

    def test_write_resistance(self, box1):
        """Setting series resistance to 10.0 should succeed."""
        result = lager_solar_resistance(box=box1, net=NET, value=10.0)
        assert "Error" not in result

    def test_temperature(self, box1):
        """Reading solar simulator temperature should succeed."""
        result = lager_solar_temperature(box=box1, net=NET)
        assert "Error" not in result

    def test_read_voc(self, box1):
        """Reading open circuit voltage should return output without errors."""
        result = lager_solar_voc(box=box1, net=NET)
        assert "Error" not in result
