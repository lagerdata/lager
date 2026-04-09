# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP resource: bench identity (box_id, hostname, version)."""

from __future__ import annotations

import json

from ..server_state import get_bench


def register(mcp):
    @mcp.resource("lager://bench/identity")
    def bench_identity() -> str:
        """Box identity: ID, hostname, version, and DUT slot summary."""
        bench = get_bench()
        return json.dumps(
            {
                "box_id": bench.box_id,
                "hostname": bench.hostname,
                "version": bench.version,
                "dut_slots": [
                    {"name": d.name, "active": d.active, "board_profile": d.board_profile}
                    for d in bench.dut_slots
                ],
            },
            indent=2,
        )
