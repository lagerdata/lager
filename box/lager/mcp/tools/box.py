# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tool for box health and configuration management."""

import json

from ..server import mcp


@mcp.tool()
def box_manage(action: str = "health") -> str:
    """Check box health or reload bench configuration.

    Args:
        action: "health" returns box ID, version, and net/instrument counts.
                "reload" re-reads config from disk and rebuilds the capability graph.
    """
    if action == "health":
        from ..config import get_box_id, get_box_version
        from ..server_state import get_bench

        bench = get_bench()
        return json.dumps({
            "status": "ok",
            "box_id": get_box_id(),
            "version": get_box_version(),
            "nets": len(bench.nets),
            "instruments": len(bench.instruments),
        })

    elif action == "reload":
        from ..server_state import reload_bench

        reload_bench()
        from ..server_state import get_bench, get_capability_graph

        bench = get_bench()
        graph = get_capability_graph()
        return json.dumps({
            "status": "ok",
            "nets": len(bench.nets),
            "instruments": len(bench.instruments),
            "capabilities": len(graph.nodes),
        })

    else:
        return json.dumps({"error": f"Unknown action '{action}'. Use 'health' or 'reload'."})
