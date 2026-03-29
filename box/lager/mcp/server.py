#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Lager MCP Server - Model Context Protocol server for Lager hardware boxes.

Architecture:
    AI Agent
        |  MCP (stdio / SSE)
        v
    Lager MCP Server (this process)
        |  direct HTTP to box services (:5000, :8080, :9000)
        v
    Lager Box

The server initializes by loading the bench definition and capability
graph from the target box, then exposes structured resources, coarse-
grained scenario tools, and fine-grained debug tools.

Scenario execution uses an **interpreter-style runner** -- a fixed
Python script uploaded to the box's Python service (:5000).  The agent
sends scenario JSON; the runner dispatches each step to on-box hardware
APIs (GPIO, SPI, power, etc.) and returns structured results.

Legacy subprocess-based tool execution is preserved for backward
compatibility via ``run_lager()``.

v0 simplifications (see also scenario_runner.py, session.py):
  - Runner script is re-uploaded per invocation
  - No box lock acquired (single-user assumption)
  - cli/mcp/ location is pragmatic; box-resident components may
    move to box/lager/mcp/ in a future version

Usage:
    # Set target box and run
    LAGER_BOX=HW-7  lager-mcp
    LAGER_BOX_IP=100.64.0.5  lager-mcp

    # Add to Claude Code
    claude mcp add --transport stdio lager -- lager-mcp
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import resolve_box_ip, resolve_box_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Create the MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "lager",
    instructions=(
        "Lager MCP server -- one server per Lager box. "
        "This server is scoped to a single hardware-in-the-loop bench. "
        "Use discovery resources (lager://bench/*) to understand the bench "
        "capabilities, then use coarse-grained scenario tools for test "
        "execution. Fine-grained tools are available for debugging."
    ),
)


# ---------------------------------------------------------------------------
# Legacy CLI subprocess helper (kept for backward-compat fine-grained tools)
# ---------------------------------------------------------------------------


def run_lager(*args: str, timeout: int = 60) -> str:
    """Run a lager CLI command and return output.

    Args:
        *args: Command-line arguments passed to the ``lager`` binary.
        timeout: Maximum seconds to wait for the command to complete.

    Returns:
        stdout on success, or an error message on failure.
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


# ---------------------------------------------------------------------------
# Direct HTTP helpers for box communication
# ---------------------------------------------------------------------------


def box_http_get(path: str, *, port: int = 5000, timeout: float = 30.0) -> dict[str, Any] | list | str:
    """GET request to the target box's HTTP API."""
    import requests
    from .server_state import get_box_ip

    ip = get_box_ip()
    if not ip:
        return {"error": "No box IP configured. Set LAGER_BOX or LAGER_BOX_IP."}

    url = f"http://{ip}:{port}{path}"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            return resp.json()
        return resp.text
    except Exception as exc:
        return {"error": str(exc)}


def box_http_post(
    path: str,
    *,
    port: int = 5000,
    json_body: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any] | list | str:
    """POST request to the target box's HTTP API."""
    import requests
    from .server_state import get_box_ip

    ip = get_box_ip()
    if not ip:
        return {"error": "No box IP configured. Set LAGER_BOX or LAGER_BOX_IP."}

    url = f"http://{ip}:{port}{path}"
    try:
        resp = requests.post(url, json=json_body, timeout=timeout)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            return resp.json()
        return resp.text
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Register resources (v0: bench identity, capabilities, netlist only)
# ---------------------------------------------------------------------------

from .resources import bench_identity  # noqa: E402
from .resources import bench_capabilities  # noqa: E402
from .resources import netlist  # noqa: E402

bench_identity.register(mcp)
bench_capabilities.register(mcp)
netlist.register(mcp)

# ---------------------------------------------------------------------------
# Register tools from submodules (legacy fine-grained + new coarse-grained)
# ---------------------------------------------------------------------------

# Legacy fine-grained tools (backward compatibility)
from .tools import box  # noqa: E402, F401
from .tools import i2c  # noqa: E402, F401
from .tools import spi  # noqa: E402, F401
from .tools import power  # noqa: E402, F401
from .tools import measurement  # noqa: E402, F401
from .tools import battery  # noqa: E402, F401
from .tools import eload  # noqa: E402, F401
from .tools import uart  # noqa: E402, F401
from .tools import usb  # noqa: E402, F401
from .tools import ble  # noqa: E402, F401
from .tools import blufi  # noqa: E402, F401
from .tools import debug  # noqa: E402, F401
from .tools import scope  # noqa: E402, F401
from .tools import logic  # noqa: E402, F401
from .tools import webcam  # noqa: E402, F401
from .tools import defaults  # noqa: E402, F401
from .tools import solar  # noqa: E402, F401
from .tools import wifi  # noqa: E402, F401
from .tools import arm  # noqa: E402, F401
from .tools import python_run  # noqa: E402, F401
from .tools import pip_tools  # noqa: E402, F401
from .tools import logs  # noqa: E402, F401
from .tools import binaries  # noqa: E402, F401

# New coarse-grained tools
from .tools import discover  # noqa: E402, F401
from .tools import scenario  # noqa: E402, F401
from .tools import firmware  # noqa: E402, F401
from .tools import observe  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Entry point for the Lager MCP server."""
    from .server_state import init_state

    box_ip = resolve_box_ip()
    if box_ip:
        logger.info("Lager MCP server targeting box at %s", box_ip)
        init_state(box_ip=box_ip)
    else:
        logger.warning(
            "No target box configured. Set LAGER_BOX or LAGER_BOX_IP. "
            "Resources will return empty data until a box is configured."
        )
        init_state()

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
