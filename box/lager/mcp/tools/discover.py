# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP discovery tools -- bench introspection and suitability reasoning."""

from __future__ import annotations

import json

from ..audit import audited
from ..server import mcp
from ..server_state import get_bench, get_capability_graph


@mcp.tool()
@audited()
def discover_bench(net_name: str | None = None) -> str:
    """Discover hardware on this bench.

    Without arguments, returns a summary: box ID, DUT slots, instruments,
    nets, interfaces, and capability counts.  With a net_name, returns
    full metadata and capabilities for that specific net.

    Args:
        net_name: Optional net to inspect (e.g. 'psu1', 'spi0').
    """
    bench = get_bench()
    graph = get_capability_graph()

    if net_name is not None:
        for net in bench.nets:
            if net.name == net_name:
                caps = graph.by_target(net_name)
                return json.dumps(
                    {
                        **net.model_dump(exclude_none=True),
                        "capabilities": [c.model_dump() for c in caps],
                    },
                    indent=2,
                )
        return json.dumps({"error": f"Net '{net_name}' not found."})

    role_counts: dict[str, int] = {}
    for node in graph.nodes:
        role_counts[node.role.value] = role_counts.get(node.role.value, 0) + 1

    nets_out = []
    for n in bench.nets:
        entry: dict = {
            "name": n.name,
            "type": n.net_type,
            "instrument": n.instrument,
            "roles": n.roles,
        }
        if n.description:
            entry["description"] = n.description
        if n.tags:
            entry["tags"] = n.tags
        nets_out.append(entry)

    summary: dict = {
        "box_id": bench.box_id,
        "hostname": bench.hostname,
        "version": bench.version,
        "dut_slots": [
            {"name": d.name, "active": d.active, "board_profile": d.board_profile}
            for d in bench.dut_slots
        ],
        "instruments": [
            {"name": i.name, "type": i.instrument_type, "connection": i.connection}
            for i in bench.instruments
        ],
        "nets": nets_out,
        "interfaces": [
            {"name": i.name, "protocol": i.protocol, "roles": i.roles}
            for i in bench.interfaces
        ],
        "capability_summary": role_counts,
        "total_capabilities": len(graph.nodes),
    }

    if not bench.nets:
        summary["warning"] = (
            "No nets configured on this box. Hardware tools will not work until "
            "nets are registered. Ask the user to run 'lager nets add-all' on "
            "the box (or 'lager nets add <name>' for individual nets) to "
            "auto-discover connected instruments, then call discover_bench() "
            "again."
        )

    return json.dumps(summary, indent=2)


@mcp.tool()
@audited()
def assess_suitability(test_type: str) -> str:
    """Assess whether this bench can run a given test type.

    Returns matched/missing capabilities, candidate nets, substitutions,
    confidence, and explanation.

    Args:
        test_type: Test type key (e.g. "qspi_flash_driver", "battery_discharge")
                   or a free-form test description.
    """
    from ..engine.heuristic_engine import assess_bench_suitability

    report = assess_bench_suitability(test_type)
    return report.model_dump_json(indent=2)
