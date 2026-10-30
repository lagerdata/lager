# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The control and exec tiers refuse on a box with no MCP bearer token (#455)."""

import asyncio
import json

import pytest

from lager.mcp.tools import _tier_guard, control
from lager.mcp.tools import exec as exec_tools


def _run(coro_fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


def _echo(net: str, count: int = 1) -> str:
    """A stand-in tool."""
    return json.dumps({"net": net, "count": count})


@pytest.mark.unit
class TestGuarded:
    def test_refuses_with_no_token_file(self, tmp_path):
        tool = _tier_guard.guarded(_echo, "control", str(tmp_path / "mcp_token"))
        result = json.loads(tool("supply1"))
        assert "no MCP bearer token" in result["error"]
        assert "control tools" in result["error"]
        assert "mcp-token enable" in result["error"]

    def test_refuses_with_an_empty_token_file(self, tmp_path):
        path = tmp_path / "mcp_token"
        path.write_text("")
        tool = _tier_guard.guarded(_echo, "exec", str(path))
        assert "error" in json.loads(tool("supply1"))

    def test_runs_with_a_token(self, tmp_path):
        path = tmp_path / "mcp_token"
        path.write_text("s3cret\n")
        tool = _tier_guard.guarded(_echo, "control", str(path))
        assert json.loads(tool("supply1", count=2)) == {"net": "supply1", "count": 2}

    def test_follows_the_token_file_without_a_restart(self, tmp_path):
        path = tmp_path / "mcp_token"
        tool = _tier_guard.guarded(_echo, "exec", str(path))
        assert "error" in json.loads(tool("a"))
        path.write_text("s3cret\n")
        assert json.loads(tool("a"))["net"] == "a"
        path.unlink()
        assert "error" in json.loads(tool("a"))

    def test_keeps_the_name_doc_and_signature(self):
        tool = _tier_guard.guarded(_echo, "control", "/nonexistent")
        assert tool.__name__ == "_echo"
        assert tool.__doc__ == _echo.__doc__


@pytest.mark.unit
class TestRegisteredTiers:
    @pytest.mark.parametrize("module,tier,names", [
        (control, "control", {"debug_probe_status", "net_status", "power_cycle_hub"}),
        (exec_tools, "exec", {"box_exec", "read_file", "write_file", "list_dir"}),
    ])
    def test_registered_tools_refuse_without_a_token(self, tmp_path, module, tier, names):
        from mcp.server.mcpserver import MCPServer

        m = MCPServer(f"test-{tier}")
        module.register(m, token_path=str(tmp_path / "mcp_token"))
        tools = {t.name: t for t in _run(m.list_tools)}
        assert names <= set(tools)
        # The wrapper must not cost the tools their argument schema.
        for name in names:
            assert tools[name].input_schema.get("properties"), name

    def test_exec_tool_does_not_run_without_a_token(self, tmp_path):
        from mcp.server.mcpserver import MCPServer

        marker = tmp_path / "ran"
        m = MCPServer("test-exec-call")
        exec_tools.register(m, token_path=str(tmp_path / "mcp_token"))
        _run(lambda: m.call_tool("box_exec", {"command": f"touch {marker}"}))
        assert not marker.exists()
