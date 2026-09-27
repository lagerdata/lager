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

The server runs as a service on the Lager box. On a box that publishes its
ports (the default) it is reachable from any MCP-compatible client at the
box's local IP address:

MCP client configuration:
    {
        "mcpServers": {
            "lager": {
                "url": "http://<box-ip>:8100/mcp"
            }
        }
    }

The server asks for no credential by default. An operator can turn on a bearer
token with ``lager box-config mcp-token enable``; the client entry then also
carries ``"headers": {"Authorization": "Bearer <token>"}``. See
``lager.mcp.auth``.

On a box started with ``--no-publish`` (see ``box/start_box.sh``), port 8100 is
NOT published on the host: the container is reachable only on the ``lagernet``
Docker network, where a reverse proxy owns the host ports. ``<box-ip>:8100``
will not connect there. Use the container's lagernet address, or whatever route
the proxy exposes -- the server itself binds ``0.0.0.0:8100`` either way, so
this is purely a question of reachability, not of the service being up.

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

from mcp.server.mcpserver import MCPServer, Context
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger(__name__)


mcp = MCPServer(
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


def connecting_host(ctx: Context | None) -> str | None:
    """Best-effort: the host the MCP client connected on, minus any port.

    The agent reaches this server at ``http://<host>:8100/mcp`` and that same
    ``<host>`` is the right value to pass to ``lager python ... --box``. We read
    it from the request ``Host`` header (falling back to the socket peer) so the
    discovery tools can hand back a *literal* runnable command instead of a
    ``<box-ip>`` placeholder.

    ``ctx`` is the per-request Context the SDK injects into a tool, and callers
    must hand it down: the v1 ``mcp.get_context()`` ambient lookup was removed
    in SDK 2.0, and a helper called by a tool never gets a Context of its own.

    Returns None when there is no HTTP request in scope (e.g. stdio transport,
    or ``ctx`` is None in unit tests), in which case callers keep the
    placeholder.
    """
    if ctx is None:
        return None

    host: str | None = None
    # Both accessors reach through ``ctx.request_context``, which raises when
    # the Context is not bound to a live request.
    try:
        headers = ctx.headers
    except Exception:
        headers = None
    if headers:
        host = headers.get("host")

    if not host:
        try:
            request = ctx.request_context.request
        except Exception:
            request = None
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

# Neither tier is announced here. This runs at import, before main() has
# configured logging: an INFO line is dropped and a WARNING comes out bare,
# with no timestamp or logger name. _log_security_posture() says both, once
# logging works.
if control_tools_enabled():
    from .tools import control  # noqa: E402

    control.register(mcp)

# General box-control primitives (arbitrary exec + file I/O) — a separate,
# more dangerous tier behind its own gate. Off by default.
if exec_tools_enabled():
    from .tools import exec as exec_tools  # noqa: E402

    exec_tools.register(mcp)

# ---------------------------------------------------------------------------
# Register prompts (slash-command entry points for MCP clients)
# ---------------------------------------------------------------------------

from . import prompts  # noqa: E402

prompts.register(mcp)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _log_security_posture(token_path=None):
    """Say, once logging works, which tiers are on and whether a token guards them.

    The one line that matters is the WARNING: a control or exec tier with no
    bearer token means anything that can reach the port can drive hardware or
    run commands on the box.
    """
    from ..box_config import mcp_token
    from ..constants import MCP_TOKEN_PATH

    token_path = MCP_TOKEN_PATH if token_path is None else token_path
    control_on, exec_on = control_tools_enabled(), exec_tools_enabled()

    if control_on:
        logger.info("Lager MCP control tools enabled (LAGER_MCP_ALLOW_CONTROL set)")
    if exec_on:
        logger.warning(
            "Lager MCP EXEC tools enabled (LAGER_MCP_ALLOW_EXEC set) — arbitrary "
            "command execution and file writes are now exposed over MCP"
        )

    state = mcp_token.state(token_path)
    if state == mcp_token.ENABLED:
        logger.info("MCP bearer token required (%s)", token_path)
    elif state in mcp_token.FAIL_CLOSED_STATES:
        logger.error(
            "MCP token file %s is %s: every request is refused until it is "
            "repaired (`lager box-config mcp-token rotate`) or removed "
            "(`lager box-config mcp-token disable`)", token_path, state,
        )
    elif control_on or exec_on:
        tiers = " and ".join(
            name for name, on in (("control", control_on), ("exec", exec_on)) if on
        )
        logger.warning(
            "Lager MCP %s tools are enabled with NO bearer token: anything that "
            "can reach this port can use them. Run `lager box-config mcp-token "
            "enable` and give the token to your MCP clients.", tiers,
        )
    else:
        logger.info("No MCP bearer token set: the MCP port is unauthenticated")


def build_app(server=None, token_path=None):
    """The ASGI app uvicorn serves: the SDK's app behind the optional bearer check.

    Separate from main() so that it can be built, and inspected, without
    binding a port.
    """
    import contextlib

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Mount

    from ..constants import MCP_TOKEN_PATH
    from .auth import BearerTokenMiddleware

    server = mcp if server is None else server
    token_path = MCP_TOKEN_PATH if token_path is None else token_path

    # SDK 2.0 moved transport config off ``mcp.settings`` (which no longer
    # carries host/port/transport_security -- assigning raises) and onto the
    # app factory. uvicorn already owns the bind below, so only the security
    # setting has to be threaded through.
    #
    # Passing it is NOT optional: with no ``transport_security``,
    # streamable_http_app() arms DNS-rebinding protection against its own
    # ``host`` default of 127.0.0.1, and every request addressed to the box's
    # LAN IP is answered with "421 Invalid Host header". The box is reached at
    # an arbitrary address on the local network, so the check is switched off
    # here rather than given an allowlist we cannot know ahead of time.
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    # streamable_http_path still defaults to "/mcp", so the documented client
    # URL (http://<box-ip>:8100/mcp) is unchanged.
    inner = server.streamable_http_app(transport_security=transport_security)

    # Taken NOW, not read off ``server`` inside the lifespan. Every
    # streamable_http_app() call makes a new session manager and points
    # ``server.session_manager`` at it, and a manager's run() works once. A
    # lifespan that looked the manager up late would, after any second build,
    # start the newer app's manager while this app's route still held the
    # unstarted one -- and every request here would fail with "Task group is
    # not initialized". Holding the pair together makes each app self-contained.
    manager = server.session_manager

    # The SDK app has a lifespan of its own that does this, but Starlette does
    # not run the lifespan of a mounted app, so it is run from the outer one.
    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with manager.run():
            yield

    return Starlette(
        routes=[Mount("/", app=inner)],
        lifespan=lifespan,
        # Off unless a token file exists; see lager.mcp.auth.
        middleware=[Middleware(BearerTokenMiddleware, token_path=token_path)],
    )


def main():
    """Start the on-box Lager MCP server."""
    import uvicorn

    from .config import MCP_PORT
    from .server_state import init_state

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    init_state()

    host = os.environ.get("LAGER_MCP_HOST", "0.0.0.0")
    logger.info("Lager MCP server starting on %s:%d (streamable-http)", host, MCP_PORT)
    _log_security_posture()

    uvicorn.run(build_app(), host=host, port=MCP_PORT)


if __name__ == "__main__":
    main()
