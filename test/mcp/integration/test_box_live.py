# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Live integration tests for box management MCP tools."""

import pytest

from lager.mcp.tools.box import (
    lager_hello,
    lager_list_nets,
    lager_instruments,
    lager_boxes_list,
    lager_boxes_export,
)


@pytest.mark.integration
@pytest.mark.box
class TestBoxLive:
    def test_hello(self, box1):
        """lager hello should return a greeting response."""
        result = lager_hello(box=box1)
        assert "Error" not in result
        # Hello output should contain a greeting or the box name
        lower = result.lower()
        assert any(kw in lower for kw in ("hello", "hi", "welcome", box1.lower())), \
            f"Expected greeting keyword in: {result!r}"

    def test_list_nets(self, box1):
        """lager nets should list net names or roles."""
        result = lager_list_nets(box=box1)
        assert "Error" not in result
        lower = result.lower()
        assert "net" in lower or "name" in lower or "role" in lower or "usb" in lower \
            or "i2c" in lower or "spi" in lower, \
            f"Expected net-related keyword in: {result!r}"

    def test_instruments(self, box1):
        """lager instruments should return a non-empty instrument list."""
        result = lager_instruments(box=box1)
        assert "Error" not in result
        assert len(result.strip()) > 0, "Instruments output should be non-empty"

    def test_boxes_list(self, box1):
        """lager boxes should return output containing the box name."""
        result = lager_boxes_list()
        assert "Error" not in result
        assert box1 in result

    def test_boxes_export(self):
        """lager boxes export should return some output (likely JSON)."""
        result = lager_boxes_export()
        assert "Error" not in result

    def test_hello_invalid_box(self):
        """lager hello with a nonexistent box should return an error string."""
        result = lager_hello(box="NONEXISTENT")
        assert "Error" in result
