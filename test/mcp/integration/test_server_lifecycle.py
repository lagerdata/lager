# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for MCP server lifecycle -- imports, tool discovery, and run_lager."""

import pytest


@pytest.mark.integration
class TestServerLifecycle:
    def test_server_imports_cleanly(self):
        """Verify the MCP server module imports without errors."""
        from lager.mcp.server import mcp, run_lager, main
        assert mcp is not None
        assert callable(run_lager)
        assert callable(main)

    def test_tool_discovery(self):
        """Verify 160+ tools are registered with unique names."""
        import asyncio
        from lager.mcp.server import mcp

        tools = asyncio.get_event_loop().run_until_complete(mcp.list_tools())
        names = [t.name for t in tools]
        assert len(names) >= 160, f"Expected 160+ tools, got {len(names)}"
        assert len(names) == len(set(names)), "Duplicate tool names found"

    def test_run_lager_with_real_cli(self):
        """Verify run_lager works with the real lager CLI."""
        from lager.mcp.server import run_lager

        result = run_lager("--version")
        # Should return version info, not a fatal error
        assert "Error" not in result or "not found" not in result
