# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP resource template: per-net-type API reference."""

from __future__ import annotations

import json


def register(mcp):
    @mcp.resource("lager://reference/{net_type}")
    def api_reference(net_type: str) -> str:
        """Python API reference for a net type (methods, gotchas, example snippet).

        Accepts enum names (PowerSupply, SPI) or raw types (power-supply, spi).
        """
        from ..data.api_reference import get_reference_for_type, list_supported_types

        ref = get_reference_for_type(net_type)
        if ref is None:
            return json.dumps({
                "error": f"No API reference for net type '{net_type}'.",
                "supported_types": list_supported_types(),
            })
        return json.dumps(ref, indent=2)
