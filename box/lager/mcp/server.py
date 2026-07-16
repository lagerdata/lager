#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Lager MCP Server — runs ON the box as a discovery and planning surface.

Architecture:
    MCP-compatible AI agent
        |  MCP (streamable-http via box IP)        ← discovery + planning only
        v
    Lager MCP Server (this process, on-box)
        |  reads /etc/lager bench config (nets, DUT context, instruments)
        v
    Bench / DUT metadata

    The agent EXECUTES tests over a separate channel — the lager CLI:
        lager python path/to/test.py --box <box-ip>

This server is read-only: it tells the agent what hardware exists and what
the DUT is, but it never drives hardware itself. All I/O happens in the test
script the agent writes and runs via ``lager python``.

The server runs as a service on the Lager box and is reachable from any
MCP-compatible client via the box's local IP address.

MCP client configuration:
    {
        "mcpServers": {
            "lager": {
                "url": "http://<box-ip>:8100/mcp"
            }
        }
    }

Primary workflow:
    1. Agent calls discover_bench() to see available hardware (and the box id
       to pass to ``--box``)
    2. Agent calls discover_dut() to learn what the DUT is and which docs to read
    3. Agent calls plan_firmware_test() with firmware description + goals
    4. Agent reads lager://guide/api-quick-reference / get_test_example() to learn the API
    5. Agent writes a Python test file locally using ``from lager import Net, NetType``
    6. Agent runs it via the lager CLI: lager python path/to/test.py --box <box-ip>
       (use the box's IP — the same address the MCP client connected on; the
       runnable may also be a folder whose entrypoint is main.py)
    7. Agent analyzes the CLI output, iterates

Full documentation (beyond the on-box guide/reference resources) lives at
https://docs.lagerdata.com — see the lager://guide/docs resource.
"""

from __future__ import annotations

import logging
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from ..box_origin import check_request as check_origin_and_host
from ..box_auth import guard as check_auth

logger = logging.getLogger(__name__)


class BrowserRejectMiddleware:
    """ASGI middleware screening requests. See lager.box_origin and lager.box_auth.

    Deliberately raw ASGI rather than Starlette's BaseHTTPMiddleware: this app
    serves streaming responses, and BaseHTTPMiddleware buffers them.

    This exists instead of the MCP SDK's own DNS-rebinding protection because
    that feature validates Host against a fixed list of literal values (plus a
    'host:*' port wildcard). A box's address is not fixed, so any list we could
    write there would eventually be wrong, and the SDK's check fails closed --
    on a machine with no console. See main() for the corresponding setting.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            # Lifespan and websocket scopes carry no Host/Origin to judge.
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode('latin-1').lower(): value.decode('latin-1')
            for key, value in scope.get('headers', [])
        }
        client = scope.get('client')
        rejection = (
            check_origin_and_host(
                headers.get('host'),
                headers.get('origin'),
                path=scope.get('path'),
                remote_addr=client[0] if client else None,
            )
            or check_auth(
                headers.get('authorization'),
                remote_addr=client[0] if client else None,
                path=scope.get('path'),
            )
        )
        if rejection is None:
            await self.app(scope, receive, send)
            return

        status, message = rejection
        body = message.encode('utf-8')
        await send({
            'type': 'http.response.start',
            'status': status,
            'headers': [
                (b'content-type', b'text/plain; charset=utf-8'),
                (b'content-length', str(len(body)).encode('ascii')),
            ],
        })
        await send({'type': 'http.response.body', 'body': body})


mcp = FastMCP(
    "lager",
    instructions=(
        "Lager hardware-in-the-loop test bench. "
        "This server is READ-ONLY: use it to DISCOVER hardware and the DUT and "
        "to PLAN tests. It does not drive hardware or run code. "
        "To EXECUTE a test, write a Python file locally using "
        "`from lager import Net, NetType`, then run it from your shell with the "
        "lager CLI: `lager python path/to/test.py --box <box-ip>` (this syncs "
        "your project to the box and runs with full project context, dtest, and "
        "all local modules). "
        "Identify the box by the IP address you connected to this MCP server on "
        "— local box names are arbitrary client-side aliases. --box accepts a "
        "raw IP directly, so no registration is needed. The runnable can be a "
        "single .py file or a folder whose entrypoint is main.py (lets you ship "
        "reusable modules). "
        "For firmware logs, RTT + defmt-print is the core debug workflow: stream "
        "from your shell with "
        "`lager debug <NET> gdbserver --box <box-ip> --rtt | defmt-print -e app.elf` "
        "(raw RTT bytes are NOT printable for defmt firmware) — read "
        "lager://guide/rtt-defmt first. "
        "Full docs beyond this server: read lager://guide/docs "
        "(https://docs.lagerdata.com)."
    ),
)


def connecting_host() -> str | None:
    """Best-effort: the host the MCP client connected on, minus any port.

    The agent reaches this server at ``http://<host>:8100/mcp`` and that same
    ``<host>`` is the right value to pass to ``lager python ... --box``. We read
    it from the request ``Host`` header (falling back to the socket peer) so the
    discovery tools can hand back a *literal* runnable command instead of a
    ``<box-ip>`` placeholder.

    Returns None when there is no HTTP request in scope (e.g. stdio transport
    or unit tests), in which case callers should keep the placeholder.
    """
    try:
        request = mcp.get_context().request_context.request
    except Exception:
        return None
    if request is None:
        return None

    host: str | None = None
    try:
        host = request.headers.get("host")
    except Exception:
        host = None
    if not host:
        client = getattr(request, "client", None)
        host = getattr(client, "host", None)
    if not host:
        return None

    host = host.strip()
    # Strip the port. Handle IPv6 literals: "[::1]:8100" -> "::1".
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


# ---------------------------------------------------------------------------
# Register resources
# ---------------------------------------------------------------------------

from .resources import bench_identity  # noqa: E402
from .resources import dut as dut_resource  # noqa: E402
from .resources import netlist  # noqa: E402
from .resources import interfaces  # noqa: E402
from .resources import guide  # noqa: E402
from .resources import api_reference as api_reference_resource  # noqa: E402

bench_identity.register(mcp)
dut_resource.register(mcp)
netlist.register(mcp)
interfaces.register(mcp)
guide.register(mcp)
api_reference_resource.register(mcp)

# ---------------------------------------------------------------------------
# Register tools
# ---------------------------------------------------------------------------

# Discovery — understand what's on this bench
from .tools import discover  # noqa: E402, F401

# DUT-level orientation — what is this box / DUT?
from .tools import dut as dut_tools  # noqa: E402, F401

# Test authoring guidance — API docs, examples, test planning
from .tools import authoring  # noqa: E402, F401

# Box health / identity
from .tools import box  # noqa: E402, F401

# Scoped box-control tools — read-only by default; only registered when an
# operator opts in via LAGER_MCP_ALLOW_CONTROL (see config.control_tools_enabled).
from .config import control_tools_enabled, exec_tools_enabled  # noqa: E402

if control_tools_enabled():
    from .tools import control  # noqa: E402

    control.register(mcp)
    logger.info("Lager MCP control tools enabled (LAGER_MCP_ALLOW_CONTROL set)")

# General box-control primitives (arbitrary exec + file I/O) — a separate,
# more dangerous tier behind its own gate. Off by default.
if exec_tools_enabled():
    from .tools import exec as exec_tools  # noqa: E402

    exec_tools.register(mcp)
    logger.warning(
        "Lager MCP EXEC tools enabled (LAGER_MCP_ALLOW_EXEC set) — arbitrary "
        "command execution and file writes are now exposed over MCP"
    )

# ---------------------------------------------------------------------------
# Register prompts (slash-command entry points for MCP clients)
# ---------------------------------------------------------------------------

from . import prompts  # noqa: E402

prompts.register(mcp)

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
    # Host and Origin are enforced by BrowserRejectMiddleware below instead of by
    # the SDK's own check, which can only match Host against fixed literals and
    # would need a list of every address this box might answer on. Note the SDK
    # still requires application/json on POST regardless of this setting, which
    # is itself a preflight-forcing protection worth keeping.
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

    uvicorn.run(BrowserRejectMiddleware(app), host=host, port=MCP_PORT)


if __name__ == "__main__":
    main()
