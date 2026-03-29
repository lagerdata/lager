# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP resource: full capability graph."""

from __future__ import annotations

from ..server_state import get_capability_graph


def register(mcp):
    @mcp.resource("lager://bench/capabilities")
    def bench_capabilities() -> str:
        """Full capability graph for the bench, as structured JSON."""
        graph = get_capability_graph()
        return graph.model_dump_json(indent=2)
