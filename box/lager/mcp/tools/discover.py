# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP discovery tools -- bench introspection and suitability reasoning."""

from __future__ import annotations

import json

from ..server import mcp
from ..server_state import get_bench, get_capability_graph


@mcp.tool()
def get_bench_summary() -> str:
    """Get a summary of this Lager box: ID, DUT, instruments, nets, and capability overview.

    Use this as the first step to understand what hardware is available.
    No parameters needed -- the server is scoped to one box.
    """
    bench = get_bench()
    graph = get_capability_graph()

    role_counts: dict[str, int] = {}
    for node in graph.nodes:
        role_counts[node.role.value] = role_counts.get(node.role.value, 0) + 1

    summary = {
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
        "nets": [
            {"name": n.name, "type": n.net_type, "instrument": n.instrument, "roles": n.roles}
            for n in bench.nets
        ],
        "interfaces": [
            {"name": i.name, "protocol": i.protocol, "roles": i.roles}
            for i in bench.interfaces
        ],
        "capability_summary": role_counts,
        "total_capabilities": len(graph.nodes),
    }
    return json.dumps(summary, indent=2)


@mcp.tool()
def get_capabilities() -> str:
    """Get the full capability graph for this bench.

    Each node describes a specific role (e.g., source_power, protocol_master)
    bound to a specific net or interface, with a confidence score.
    """
    graph = get_capability_graph()
    return graph.model_dump_json(indent=2)


@mcp.tool()
def get_net_details(net_name: str) -> str:
    """Get detailed information about a specific net.

    Args:
        net_name: The net name (e.g., 'psu1', 'spi0', 'debug1')
    """
    bench = get_bench()
    graph = get_capability_graph()

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


@mcp.tool()
def get_interface_details(interface_name: str) -> str:
    """Get detailed information about a protocol interface.

    Args:
        interface_name: Interface name (e.g., 'spi0', 'i2c1', 'uart0')
    """
    bench = get_bench()
    graph = get_capability_graph()

    for iface in bench.interfaces:
        if iface.name == interface_name:
            caps = graph.by_target(interface_name)
            return json.dumps(
                {
                    **iface.model_dump(exclude_none=True),
                    "capabilities": [c.model_dump() for c in caps],
                },
                indent=2,
            )
    return json.dumps({"error": f"Interface '{interface_name}' not found."})


@mcp.tool()
def infer_test_requirements(test_description: str) -> str:
    """Infer what bench capabilities are needed for a given test.

    Given a natural-language test description, returns required,
    recommended, and optional capabilities.

    Args:
        test_description: Description of the test (e.g., "QSPI flash driver validation")
    """
    from ..engine.heuristic_engine import infer_requirements

    req = infer_requirements(test_description)
    return req.model_dump_json(indent=2)


@mcp.tool()
def assess_suitability(test_type: str) -> str:
    """Assess whether this bench can run a given test type.

    Returns a suitability report with matched/missing capabilities,
    candidate nets, substitutions, confidence, and explanation.

    Args:
        test_type: Test type key (e.g., "qspi_flash_driver", "spi_slave_validation",
                   "battery_discharge") or a free-form test description.
    """
    from ..engine.heuristic_engine import assess_bench_suitability

    report = assess_bench_suitability(test_type)
    return report.model_dump_json(indent=2)


@mcp.tool()
def find_candidate_nets(role: str) -> str:
    """Find nets that can serve a given capability role.

    Args:
        role: Capability role to search for (e.g., "source_power",
              "protocol_master", "flash_firmware", "capture_waveform")
    """
    from ..schemas.capability import CapabilityRole

    graph = get_capability_graph()

    try:
        cap_role = CapabilityRole(role)
    except ValueError:
        return json.dumps({"error": f"Unknown role: '{role}'", "valid_roles": [r.value for r in CapabilityRole]})

    nodes = graph.by_role(cap_role)
    results = [
        {"target": n.target, "confidence": n.confidence, "parameters": n.parameters, "notes": n.notes}
        for n in nodes
    ]
    return json.dumps({"role": role, "candidates": results}, indent=2)
