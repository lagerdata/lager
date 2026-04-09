#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Lager MCP Server — runs ON the box with direct hardware access.

Architecture:
    AI Agent (Cursor)
        |  MCP (streamable-http via box IP)
        v
    Lager MCP Server (this process, on-box)
        |  direct lager.Net API
        v
    Hardware (power supplies, debug probes, GPIO, protocols, etc.)

The server runs as a service on the Lager box and is reachable from
Cursor via the box's local IP address.  All hardware operations execute
directly on-box with no round trips back to the agent.

Cursor MCP configuration:
    {
        "mcpServers": {
            "lager": {
                "url": "http://<box-ip>:8100/mcp"
            }
        }
    }

Primary workflow:
    1. Agent calls discover_bench() to see available hardware
    2. Agent calls plan_firmware_test() with firmware description + goals
    3. Agent reads lager://reference/{net_type} / get_test_example() to learn the API
    4. Agent writes a Python test file locally using ``from lager import Net, NetType``
    5. Agent runs via shell: lager python --serial <BOX> path/to/test.py
    6. Agent analyzes results, iterates
"""

from __future__ import annotations

import logging
import os
import subprocess

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger(__name__)

# Generic CLI passthrough is OFF by default in shared/multi-tenant environments.
# Set LAGER_MCP_ALLOW_RUN_LAGER=1 on the box to enable. Stout and other control
# planes should keep this disabled and use their own dispatch path with audit
# and per-org RBAC.
LAGER_MCP_ALLOW_RUN_LAGER = os.environ.get("LAGER_MCP_ALLOW_RUN_LAGER", "0") == "1"


def run_lager(*args: str, timeout: int = 60) -> str:
    """Run a lager CLI command and return output.

    DANGER: generic shell into the lager CLI. Disabled by default; set
    LAGER_MCP_ALLOW_RUN_LAGER=1 to enable. For test execution, write a
    Python file and run it via: lager python --serial <BOX> path/to/test.py

    Still used by tool modules that shell out to the lager CLI
    (python_run, pip_tools, logs, defaults, binaries) when enabled.
    Converted tools use the direct lager.Net API instead.
    """
    if not LAGER_MCP_ALLOW_RUN_LAGER:
        return (
            "Error: run_lager is disabled. Set LAGER_MCP_ALLOW_RUN_LAGER=1 "
            "on the box to enable. For test execution, write a Python file "
            "and run it via: lager python --serial <BOX> path/to/test.py"
        )
    try:
        result = subprocess.run(
            ["lager"] + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return (
            "Error: 'lager' CLI not found. "
            "Install with: cd cli && pip install -e ."
        )
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"

    output = result.stdout.strip()
    errors = result.stderr.strip()

    if result.returncode != 0:
        parts = []
        if output:
            parts.append(output)
        if errors:
            parts.append(errors)
        return f"Error (exit {result.returncode}): {' | '.join(parts) or 'unknown error'}"

    if errors and output:
        return f"{output}\n\n[warnings] {errors}"
    return output or "(no output)"


mcp = FastMCP(
    "lager",
    instructions=(
        "Lager hardware-in-the-loop test bench. "
        "Use this server to DISCOVER hardware and PLAN tests. "
        "To EXECUTE tests, write a Python file locally and run it via shell: "
        "lager python --serial <BOX> path/to/test.py "
        "(this syncs your project to the box and runs with full project context, "
        "dtest, and all local modules). "
        "Use quick_io() only for simple spot-checks on a single net."
    ),
)

# ---------------------------------------------------------------------------
# Register resources
# ---------------------------------------------------------------------------

from .resources import bench_identity  # noqa: E402
from .resources import netlist  # noqa: E402
from .resources import interfaces  # noqa: E402
from .resources import guide  # noqa: E402
from .resources import api_reference as api_reference_resource  # noqa: E402

bench_identity.register(mcp)
netlist.register(mcp)
interfaces.register(mcp)
guide.register(mcp)
api_reference_resource.register(mcp)

# ---------------------------------------------------------------------------
# Register tools
# ---------------------------------------------------------------------------

# Discovery — understand what's on this bench
from .tools import discover  # noqa: E402, F401

# Test authoring guidance — API docs, examples, test planning
from .tools import authoring  # noqa: E402, F401

# Package management
from .tools import scenario  # noqa: E402, F401

# Quick debug tools — interactive spot-checks
from .tools import quick  # noqa: E402, F401

# Box health
from .tools import box  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Start the on-box Lager MCP server."""
    import contextlib

    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from .config import MCP_PORT
    from .server_state import init_state

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    init_state()

    host = os.environ.get("LAGER_MCP_HOST", "0.0.0.0")
    logger.info("Lager MCP server starting on %s:%d (streamable-http)", host, MCP_PORT)

    mcp.settings.host = host
    mcp.settings.port = MCP_PORT
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[Mount("/", app=mcp.streamable_http_app())],
        lifespan=lifespan,
    )

    uvicorn.run(app, host=host, port=MCP_PORT)


if __name__ == "__main__":
    main()
