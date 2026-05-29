# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""MCP resource: bench identity (box_id, hostname, version, DUT purpose)."""

from __future__ import annotations

import json

from ..server_state import get_bench


def register(mcp):
    @mcp.resource("lager://bench/identity")
    def bench_identity() -> str:
        """Box identity: ID, hostname, version, and a summary of each DUT slot.

        Each DUT slot summary includes the ``purpose`` line so an agent can
        tell at a glance *what this box tests* without fetching the full
        DUT context resource.
        """
        bench = get_bench()
        return json.dumps(
            {
                "box_id": bench.box_id,
                "hostname": bench.hostname,
                "version": bench.version,
                "dut_slots": [
                    {
                        "name": d.name,
                        "active": d.active,
                        "board_profile": d.board_profile,
                        "purpose": d.purpose,
                        "mcu": d.mcu,
                    }
                    for d in bench.dut_slots
                ],
                "more": {
                    "dut_context": "lager://dut/context",
                    "dut_overview": "lager://dut/overview.md",
                },
            },
            indent=2,
        )
