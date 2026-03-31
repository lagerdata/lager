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
    1. Agent reads lager://bench/* resources to understand hardware
    2. Agent calls run_scenario() with a multi-step test plan
    3. ALL steps execute on-box in sequence — one round trip total
    4. Agent analyzes structured results and iterates

Fine-grained tools (supply_*, debug_*, spi_*, etc.) exist for
interactive debugging but each one costs an agent round trip.
"""

from __future__ import annotations

import logging
import os
import subprocess

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger(__name__)


def run_lager(*args: str, timeout: int = 60) -> str:
    """Run a lager CLI command and return output.

    Still used by tool modules that shell out to the lager CLI
    (python_run, pip_tools, logs, defaults, binaries).  Converted tools
    use the direct lager.Net API instead.
    """
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
        "Lager MCP server — runs on a Lager box with direct hardware access. "
        "This server is scoped to a single hardware-in-the-loop bench.\n\n"
        "WORKFLOW:\n"
        "1. Use get_bench_summary() to understand available hardware\n"
        "2. Use assess_suitability() to check if the bench can run your test\n"
        "3. Use run_scenario() for multi-step operations — this executes ALL "
        "steps on-box with no round trips, keeping latency low\n"
        "4. Use fine-grained tools (supply_*, debug_*, spi_*) only for "
        "one-off debugging, not for production test sequences\n\n"
        "IMPORTANT: Prefer run_scenario() over sequential fine-grained tool "
        "calls. A scenario with 10 hardware steps runs in one round trip. "
        "10 fine-grained calls would cost 10 round trips."
    ),
)

# ---------------------------------------------------------------------------
# Register resources
# ---------------------------------------------------------------------------

from .resources import bench_identity  # noqa: E402
from .resources import bench_capabilities  # noqa: E402
from .resources import netlist  # noqa: E402
from .resources import interfaces  # noqa: E402
from .resources import safety_constraints  # noqa: E402

bench_identity.register(mcp)
bench_capabilities.register(mcp)
netlist.register(mcp)
interfaces.register(mcp)
safety_constraints.register(mcp)

# ---------------------------------------------------------------------------
# Register tools — coarse-grained (primary) + fine-grained (debug)
# ---------------------------------------------------------------------------

# Coarse-grained: discovery, scenarios, firmware, observation
from .tools import discover  # noqa: E402, F401
from .tools import scenario  # noqa: E402, F401
from .tools import firmware  # noqa: E402, F401
from .tools import observe  # noqa: E402, F401

# Fine-grained: direct hardware access (each call = one agent round trip)
from .tools import power  # noqa: E402, F401
from .tools import debug  # noqa: E402, F401
from .tools import uart  # noqa: E402, F401
from .tools import spi  # noqa: E402, F401
from .tools import i2c  # noqa: E402, F401
from .tools import box  # noqa: E402, F401
from .tools import measurement  # noqa: E402, F401
from .tools import usb  # noqa: E402, F401
from .tools import battery  # noqa: E402, F401
from .tools import eload  # noqa: E402, F401
from .tools import scope  # noqa: E402, F401
from .tools import logic  # noqa: E402, F401

# Converted from run_lager() → direct on-box API
from .tools import solar  # noqa: E402, F401
from .tools import arm  # noqa: E402, F401
from .tools import webcam  # noqa: E402, F401
from .tools import ble  # noqa: E402, F401
from .tools import blufi  # noqa: E402, F401
from .tools import wifi  # noqa: E402, F401

# New tool modules (NetTypes with no prior coverage)
from .tools import energy  # noqa: E402, F401
from .tools import router  # noqa: E402, F401

# PicoScope streaming (daemon-based, separate from Rigol scope tools)
from .tools import scope_stream  # noqa: E402, F401

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
