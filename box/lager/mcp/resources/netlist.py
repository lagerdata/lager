# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP resource: full netlist with metadata."""

from __future__ import annotations

import json

from ..server_state import get_bench


def register(mcp):
    @mcp.resource("lager://bench/netlist")
    def bench_netlist() -> str:
        """All nets on the bench with type, roles, instrument, and limits."""
        bench = get_bench()
        nets = [n.model_dump(exclude_none=True) for n in bench.nets]
        return json.dumps(nets, indent=2)
