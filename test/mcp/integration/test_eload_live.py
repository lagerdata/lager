# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Live integration tests for electronic load MCP tools."""

import pytest

from lager.mcp.tools.eload import (
    lager_eload_cc,
    lager_eload_cv,
    lager_eload_cr,
    lager_eload_cp,
    lager_eload_state,
)

NET = "eload1"


@pytest.mark.integration
@pytest.mark.eload
class TestEloadLive:

    @pytest.fixture(autouse=True)
    def safety_teardown(self, box1):
        """Always set eload to safe state (CC 0A) after each test."""
        yield
        lager_eload_cc(box=box1, net=NET, value=0)

    def test_read_cc(self, box1):
        """Reading constant current setting should return output without errors."""
        result = lager_eload_cc(box=box1, net=NET)
        assert "Error" not in result

    def test_set_cc(self, box1):
        """Setting constant current to 0.1A should succeed."""
        result = lager_eload_cc(box=box1, net=NET, value=0.1)
        assert "Error" not in result

    def test_read_cv(self, box1):
        """Reading constant voltage setting should return output without errors."""
        result = lager_eload_cv(box=box1, net=NET)
        assert "Error" not in result

    def test_read_state(self, box1):
        """Reading eload state should return output without errors."""
        result = lager_eload_state(box=box1, net=NET)
        assert "Error" not in result

    def test_cr_cp_read(self, box1):
        """Reading constant resistance and constant power should succeed."""
        cr_result = lager_eload_cr(box=box1, net=NET)
        assert "Error" not in cr_result

        cp_result = lager_eload_cp(box=box1, net=NET)
        assert "Error" not in cp_result

    def test_set_cv(self, box1):
        """Setting constant voltage to 5.0V should succeed."""
        result = lager_eload_cv(box=box1, net=NET, value=5.0)
        assert "Error" not in result

    def test_set_cr(self, box1):
        """Setting constant resistance to 100.0 ohm should succeed."""
        result = lager_eload_cr(box=box1, net=NET, value=100.0)
        assert "Error" not in result

    def test_set_cp(self, box1):
        """Setting constant power to 10.0W should succeed."""
        result = lager_eload_cp(box=box1, net=NET, value=10.0)
        assert "Error" not in result
