# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Live integration tests for SPI MCP tools."""

import pytest

from lager.mcp.tools.spi import (
    lager_spi_list_nets,
    lager_spi_config,
    lager_spi_transfer,
    lager_spi_read,
)


@pytest.mark.integration
@pytest.mark.spi
class TestSPILive:

    def test_list_nets(self, box3):
        """Listing SPI nets should return output containing SPI-related info."""
        result = lager_spi_list_nets(box=box3)
        assert "Error" not in result
        assert "spi" in result.lower(), \
            f"Expected 'spi' in list_nets output: {result!r}"

    def test_config_mode_frequency(self, box3):
        """Configuring SPI mode 0 and frequency 1M should succeed."""
        result = lager_spi_config(box=box3, net="spi1", mode="0", frequency="1M")
        assert "Error" not in result

    def test_config_show(self, box3):
        """Showing SPI config should return mode/frequency information."""
        result = lager_spi_config(box=box3, net="spi1")
        assert "Error" not in result
        lower = result.lower()
        assert "mode" in lower or "frequency" in lower or "freq" in lower, \
            f"Expected config keywords in: {result!r}"

    def test_transfer(self, box3):
        """Transferring data should return hex-like content."""
        result = lager_spi_transfer(box=box3, net="spi1", num_words=4, data="0xFF")
        assert "Error" not in result
        # SPI transfer should return some hex data
        has_hex = "0x" in result or any(c in result for c in "0123456789abcdef")
        assert has_hex, f"Expected hex content in transfer output: {result!r}"

    def test_read(self, box3):
        """Reading SPI data should return hex-like content."""
        result = lager_spi_read(box=box3, net="spi1", num_words=4)
        assert "Error" not in result
        has_hex = "0x" in result or any(c in result for c in "0123456789abcdef")
        assert has_hex, f"Expected hex content in read output: {result!r}"
