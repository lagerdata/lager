# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Live integration tests for USB hub MCP tools."""

import pytest

from lager.mcp.tools.usb import (
    lager_usb_enable,
    lager_usb_disable,
    lager_usb_toggle,
)

NET = "usb1"


@pytest.mark.integration
@pytest.mark.usb
class TestUSBLive:

    @pytest.fixture(autouse=True)
    def safety_teardown(self, box3):
        """Ensure USB port is left enabled after each test."""
        yield
        try:
            lager_usb_enable(box=box3, net=NET)
        except Exception:
            pass

    def test_enable(self, box3):
        """Enabling USB port should succeed."""
        result = lager_usb_enable(box=box3, net=NET)
        assert "Error" not in result

    def test_disable(self, box3):
        """Disabling USB port should succeed."""
        result = lager_usb_disable(box=box3, net=NET)
        assert "Error" not in result

    def test_toggle(self, box3):
        """Toggling USB port should succeed."""
        result = lager_usb_toggle(box=box3, net=NET)
        assert "Error" not in result

    def test_enable_disable_cycle(self, box3):
        """A full enable-disable cycle should succeed."""
        enable_result = lager_usb_enable(box=box3, net=NET)
        assert "Error" not in enable_result

        disable_result = lager_usb_disable(box=box3, net=NET)
        assert "Error" not in disable_result

    def test_invalid_net(self, box3):
        """Enabling a nonexistent USB net should return an error."""
        result = lager_usb_enable(box=box3, net="usb_nonexistent_99")
        assert "Error" in result or "error" in result.lower() or "not found" in result.lower(), \
            f"Expected error for invalid net: {result!r}"
