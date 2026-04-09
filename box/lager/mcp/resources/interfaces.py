# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP resource: protocol interface descriptors."""

from __future__ import annotations

import json

from ..server_state import get_bench


def register(mcp):
    @mcp.resource("lager://bench/interfaces")
    def bench_interfaces() -> str:
        """Protocol interfaces (SPI, I2C, UART, JTAG) with constituent nets and roles."""
        bench = get_bench()
        ifaces = [i.model_dump(exclude_none=True) for i in bench.interfaces]
        return json.dumps(ifaces, indent=2)
