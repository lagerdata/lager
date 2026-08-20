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


class _StubCtx:
    """Stands in for the SDK Context injected into a tool.

    Only the two accessors connecting_host() reads are modelled. Both raise on
    a real Context that is not bound to a live request, so ``raises`` covers
    that path without needing a server.
    """

    def __init__(self, headers=None, peer=None, raises=False):
        self._headers = headers
        self._peer = peer
        self._raises = raises

    @property
    def headers(self):
        if self._raises:
            raise ValueError("Context is not available outside of a request")
        return self._headers

    @property
    def request_context(self):
        if self._raises:
            raise ValueError("Context is not available outside of a request")
        client = type("Client", (), {"host": self._peer})() if self._peer else None
        request = type("Request", (), {"client": client})()
        return type("RequestContext", (), {"request": request})()


@pytest.mark.unit
class TestConnectingHost:
    def test_returns_none_without_ctx(self):
        """connecting_host() must degrade to None when there's no Context.

        SDK 2.0 removed the ambient mcp.get_context(), so a caller with no
        request in scope (stdio transport, unit tests) passes None and the
        discovery tools keep the <box-ip> placeholder.
        """
        from lager.mcp.server import connecting_host

        assert connecting_host(None) is None

    def test_returns_none_outside_request(self):
        """A Context not bound to a live request must not propagate its raise."""
        from lager.mcp.server import connecting_host

        assert connecting_host(_StubCtx(raises=True)) is None

    def test_returns_none_when_transport_carries_no_headers(self):
        """stdio gives headers=None and no socket peer."""
        from lager.mcp.server import connecting_host

        assert connecting_host(_StubCtx(headers=None)) is None

    @pytest.mark.parametrize(
        "header,expected",
        [
            ("192.168.1.50:8100", "192.168.1.50"),  # the on-box case
            ("192.168.1.50", "192.168.1.50"),       # no port
            ("[::1]:8100", "::1"),                  # IPv6 literal with port
            ("[fe80::1]", "fe80::1"),               # IPv6 literal, no port
            ("  box.local:8100  ", "box.local"),    # surrounding whitespace
        ],
    )
    def test_strips_port_from_host_header(self, header, expected):
        """The Host header is what the agent connected on -- minus any port."""
        from lager.mcp.server import connecting_host

        assert connecting_host(_StubCtx(headers={"host": header})) == expected

    def test_falls_back_to_socket_peer(self):
        """No Host header: use the socket peer rather than giving up."""
        from lager.mcp.server import connecting_host

        assert connecting_host(_StubCtx(headers={}, peer="10.0.0.4")) == "10.0.0.4"

    def test_host_header_wins_over_socket_peer(self):
        """The address the agent addressed beats the address it came from."""
        from lager.mcp.server import connecting_host

        ctx = _StubCtx(headers={"host": "192.168.1.50:8100"}, peer="10.0.0.4")
        assert connecting_host(ctx) == "192.168.1.50"
