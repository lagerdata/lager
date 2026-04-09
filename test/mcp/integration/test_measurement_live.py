# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Live integration tests for measurement MCP tools (ADC, DAC, GPIO, watt)."""

import re

import pytest

from lager.mcp.tools.measurement import (
    lager_adc_read,
    lager_dac_write,
    lager_gpi_read,
    lager_gpo_set,
    lager_watt_read,
    lager_gpi_wait_for,
)


@pytest.mark.integration
@pytest.mark.measurement
class TestMeasurementLive:

    @pytest.fixture(autouse=True)
    def safety_teardown(self, box3):
        """Always reset DAC to 0V after each test."""
        yield
        lager_dac_write(box=box3, net="dac1", voltage=0.0)

    def test_adc_read(self, box3):
        """Reading ADC channel adc1 should return a numeric value."""
        result = lager_adc_read(box=box3, net="adc1")
        assert "Error" not in result
        # Result should contain a number (digits and/or decimal point)
        assert re.search(r"\d+\.?\d*", result), f"Expected numeric output, got: {result}"

    def test_adc_read_multiple_channels(self, box3):
        """Reading ADC channels adc1 and adc2 should both return numeric values."""
        result1 = lager_adc_read(box=box3, net="adc1")
        assert "Error" not in result1
        assert re.search(r"\d+\.?\d*", result1), f"Expected numeric output for adc1, got: {result1}"

        result2 = lager_adc_read(box=box3, net="adc2")
        assert "Error" not in result2
        assert re.search(r"\d+\.?\d*", result2), f"Expected numeric output for adc2, got: {result2}"

    def test_dac_read(self, box3):
        """Reading DAC channel dac1 should return output without errors."""
        result = lager_dac_write(box=box3, net="dac1")
        assert "Error" not in result

    def test_dac_set(self, box3):
        """Setting DAC output to 2.5V should succeed."""
        result = lager_dac_write(box=box3, net="dac1", voltage=2.5)
        assert "Error" not in result

    def test_dac_readback(self, box3):
        """Setting DAC then reading back should return a numeric value."""
        set_result = lager_dac_write(box=box3, net="dac1", voltage=2.5)
        assert "Error" not in set_result

        read_result = lager_dac_write(box=box3, net="dac1")
        assert "Error" not in read_result
        # Readback should contain a number
        assert re.search(r"\d+\.?\d*", read_result), f"Expected numeric readback, got: {read_result}"

    def test_gpi_read(self, box3):
        """Reading GPIO input gpio1 should return output without errors."""
        result = lager_gpi_read(box=box3, net="gpio1")
        assert "Error" not in result

    def test_gpo_set_high_low(self, box3):
        """Setting GPIO output high then low should both return non-empty output."""
        high_result = lager_gpo_set(box=box3, net="gpio28", level="high")
        assert "Error" not in high_result
        assert len(high_result.strip()) > 0, "Expected non-empty output for GPO high"

        low_result = lager_gpo_set(box=box3, net="gpio28", level="low")
        assert "Error" not in low_result
        assert len(low_result.strip()) > 0, "Expected non-empty output for GPO low"

    def test_gpo_set_toggle(self, box3):
        """Toggling GPIO output should succeed."""
        result = lager_gpo_set(box=box3, net="gpio28", level="toggle")
        assert "Error" not in result

    # NOTE: test_gpo_hold removed -- GPO --hold blocks indefinitely by design,
    # so it always hits run_lager's 60s timeout and may leave the LabJack in a
    # claimed state that breaks subsequent tests.  The --hold flag is verified
    # by the unit tests (cli arg construction) which is sufficient.

    def test_watt_read(self, box1):
        """Reading watt meter watt1 should return output without errors."""
        result = lager_watt_read(box=box1, net="watt1")
        assert "Error" not in result

    def test_gpi_wait_for_timeout(self, box3):
        """Waiting for GPIO with a short timeout should complete (may time out)."""
        # Use a very short timeout so the test doesn't hang.
        # The command will either detect the level or time out -- both are
        # valid outcomes.  We just verify run_lager returns *something*.
        result = lager_gpi_wait_for(box=box3, net="gpio1", level="high", timeout=2.0)
        assert result is not None
        assert len(result) > 0
