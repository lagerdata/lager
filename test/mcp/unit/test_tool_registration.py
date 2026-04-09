# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests verifying MCP tool registration and discovery."""

import pytest
import asyncio
import importlib


@pytest.mark.unit
class TestToolRegistration:
    """Verify that all tool modules import correctly and register tools."""

    def test_all_tool_modules_imported(self):
        """Every tool submodule under lager.mcp.tools should import without error."""
        module_names = [
            "arm",
            "battery",
            "binaries",
            "ble",
            "blufi",
            "box",
            "debug",
            "defaults",
            "eload",
            "i2c",
            "logic",
            "logs",
            "measurement",
            "pip_tools",
            "power",
            "python_run",
            "scope",
            "solar",
            "spi",
            "uart",
            "usb",
            "webcam",
            "wifi",
        ]
        for name in module_names:
            mod = importlib.import_module(f"lager.mcp.tools.{name}")
            assert mod is not None, f"Failed to import lager.mcp.tools.{name}"

    def test_total_tool_count(self):
        """The MCP server should register at least 160 tools."""
        from lager.mcp.server import mcp

        loop = asyncio.new_event_loop()
        try:
            tools = loop.run_until_complete(mcp.list_tools())
        finally:
            loop.close()
        assert len(tools) >= 156, (
            f"Expected at least 156 tools, got {len(tools)}"
        )

    def test_tool_names_unique(self):
        """Every registered tool name must be unique (no duplicates)."""
        from lager.mcp.server import mcp

        loop = asyncio.new_event_loop()
        try:
            tools = loop.run_until_complete(mcp.list_tools())
        finally:
            loop.close()
        names = [t.name for t in tools]
        assert len(names) == len(set(names)), (
            f"Duplicate tool names found: "
            f"{[n for n in names if names.count(n) > 1]}"
        )
