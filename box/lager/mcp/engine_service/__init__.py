# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The bench engine as a stateless HTTP service, for a host that stores manifests.

Every request carries the manifest(s) it is about, so the service holds no
state and needs no box: a fleet host feeds it what boxes published with
``GET /bench`` and gets the same answers the engine gives on a box. Run it
with ``python -m lager.mcp.engine_service``.

Imports only the pure engine (``engine.fleet`` and what it uses) plus
Starlette and uvicorn: never the MCP server, the tools, the live state or a
driver. ``test_engine_service_isolation`` enforces that.
"""

from .app import build_app

__all__ = ["build_app"]
