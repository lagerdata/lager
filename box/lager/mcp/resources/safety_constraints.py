# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP resource: safety constraints for the bench."""

from __future__ import annotations

import json

from ..server_state import get_bench


def register(mcp):
    @mcp.resource("lager://bench/safety")
    def bench_safety() -> str:
        """Safety constraints: per-net voltage/current limits, dangerous actions."""
        bench = get_bench()
        if bench.constraints:
            return bench.constraints.model_dump_json(indent=2)
        return json.dumps({"note": "No safety constraints configured for this bench."})
