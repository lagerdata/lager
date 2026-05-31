# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests verifying MCP tool registration and discovery.

The Lager MCP server is a READ-ONLY discovery and planning surface. It does
not drive hardware or run code — execution happens over the lager CLI
(``lager python path/to/test.py --box <box-ip>``). These tests pin the live
tool surface so it can't silently grow back into per-instrument I/O tools.
"""

import asyncio
import importlib

import pytest

# The complete set of tools the server is expected to register. Keep this in
# sync with the @mcp.tool() decorators under lager.mcp.tools.
EXPECTED_TOOLS = {
    "assess_suitability",
    "box_manage",
    "cite_schematic",
    "discover_bench",
    "discover_dut",
    "get_test_example",
    "plan_firmware_test",
}

# The live tool modules under lager.mcp.tools (discovery/planning only).
EXPECTED_TOOL_MODULES = {
    "authoring",
    "box",
    "discover",
    "dut",
}


# Prompt templates (slash-command entry points) the server should register.
EXPECTED_PROMPTS = {
    "assess_test_feasibility",
    "explore_bench",
    "write_lager_test",
}


def _run(coro_fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


def _list_tools():
    from lager.mcp.server import mcp

    return _run(mcp.list_tools)


@pytest.mark.unit
class TestToolRegistration:
    def test_live_tool_modules_import(self):
        """Every live tool submodule imports without error."""
        for name in sorted(EXPECTED_TOOL_MODULES):
            mod = importlib.import_module(f"lager.mcp.tools.{name}")
            assert mod is not None, f"Failed to import lager.mcp.tools.{name}"

    def test_registered_tools_match_expected_surface(self):
        """The server registers exactly the read-only discovery/planning tools."""
        names = {t.name for t in _list_tools()}
        assert names == EXPECTED_TOOLS, (
            f"Tool surface drift. "
            f"Unexpected: {sorted(names - EXPECTED_TOOLS)}; "
            f"Missing: {sorted(EXPECTED_TOOLS - names)}"
        )

    def test_tool_names_unique(self):
        """Every registered tool name must be unique (no duplicates)."""
        names = [t.name for t in _list_tools()]
        assert len(names) == len(set(names)), (
            f"Duplicate tool names found: "
            f"{sorted(n for n in names if names.count(n) > 1)}"
        )

    def test_no_io_or_mutation_tools_registered(self):
        """Guard against execution/mutation tools creeping back in."""
        names = {t.name for t in _list_tools()}
        forbidden = {
            "quick_io",
            "install_dependency",
            "run_python",
            "run_lager",
            "preflight_check",
        }
        assert not (names & forbidden), (
            f"Read-only server must not expose I/O tools: {sorted(names & forbidden)}"
        )

    def test_registered_prompts_match_expected(self):
        """The server exposes the slash-command prompt entry points."""
        from lager.mcp.server import mcp

        names = {p.name for p in _run(mcp.list_prompts)}
        assert names == EXPECTED_PROMPTS, (
            f"Prompt drift. "
            f"Unexpected: {sorted(names - EXPECTED_PROMPTS)}; "
            f"Missing: {sorted(EXPECTED_PROMPTS - names)}"
        )


@pytest.mark.unit
class TestConnectingHost:
    def test_returns_none_outside_request(self):
        """connecting_host() must degrade to None when there's no HTTP request."""
        from lager.mcp.server import connecting_host

        assert connecting_host() is None
