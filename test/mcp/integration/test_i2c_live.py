# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Live integration tests for I2C MCP tools."""

import re

import pytest

from lager.mcp.tools.i2c import (
    lager_i2c_list_nets,
    lager_i2c_scan,
    lager_i2c_config,
)


@pytest.mark.integration
@pytest.mark.i2c
class TestI2CLive:

    def test_list_nets(self, box3):
        """Listing I2C nets should mention i2c in the output."""
        result = lager_i2c_list_nets(box=box3)
        assert "Error" not in result
        assert re.search(r"i2c", result, re.IGNORECASE), \
            f"Expected 'i2c' in net listing, got: {result}"

    def test_scan_ft232h(self, box3):
        """Scanning I2C bus i2c1 (FT232H) should return addresses or 'No devices'."""
        result = lager_i2c_scan(box=box3, net="i2c1")
        assert "Error" not in result
        # Scan output should contain hex addresses (0x..) or a "no devices" message
        has_hex = re.search(r"0x[0-9a-fA-F]{2}", result)
        has_no_devices = re.search(r"[Nn]o devices", result)
        assert has_hex or has_no_devices, \
            f"Expected hex addresses or 'No devices' in scan, got: {result}"

    def test_scan_labjack(self, box3):
        """Scanning I2C bus i2c2 (LabJack) should return addresses or 'No devices'."""
        result = lager_i2c_scan(box=box3, net="i2c2")
        assert "Error" not in result
        has_hex = re.search(r"0x[0-9a-fA-F]{2}", result)
        has_no_devices = re.search(r"[Nn]o devices", result)
        assert has_hex or has_no_devices, \
            f"Expected hex addresses or 'No devices' in scan, got: {result}"

    def test_scan_custom_range(self, box3):
        """Scanning with a custom address range should return addresses or 'No devices'."""
        result = lager_i2c_scan(box=box3, net="i2c1", start="0x00", end="0x7F")
        assert "Error" not in result
        has_hex = re.search(r"0x[0-9a-fA-F]{2}", result)
        has_no_devices = re.search(r"[Nn]o devices", result)
        assert has_hex or has_no_devices, \
            f"Expected hex addresses or 'No devices' in scan, got: {result}"

    def test_config_frequency(self, box3):
        """Configuring I2C frequency to 100k should succeed."""
        result = lager_i2c_config(box=box3, net="i2c1", frequency="100k")
        assert "Error" not in result

    def test_config_show(self, box3):
        """Showing I2C config (no params) should return output without errors."""
        result = lager_i2c_config(box=box3, net="i2c1")
        assert "Error" not in result
