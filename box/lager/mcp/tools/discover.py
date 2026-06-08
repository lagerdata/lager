# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""MCP discovery tools -- bench introspection and suitability reasoning."""

from __future__ import annotations

import json

from ..server import connecting_host, mcp
from ..server_state import get_bench, get_capability_graph


def _instrument_entry(inst) -> dict:
    """Render an instrument for discovery, including the detail an agent needs
    to write a *valid* test (channels, capabilities, ranges) — not just its
    name and connection. Empty fields are omitted to keep the payload tight.
    """
    entry: dict = {
        "name": inst.name,
        "type": inst.instrument_type,
        "connection": inst.connection,
    }
    if inst.channels:
        entry["channels"] = inst.channels
    if inst.capabilities:
        entry["capabilities"] = inst.capabilities
    if inst.firmware_version:
        entry["firmware_version"] = inst.firmware_version
    if inst.metadata:
        # metadata is where authored specs/ranges live (e.g. max_voltage).
        entry["metadata"] = inst.metadata
    return entry


@mcp.tool()
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
                payload: dict = {
                    **net.model_dump(exclude_none=True),
                    "capabilities": [c.model_dump() for c in caps],
                }
                # Attach DUT subsystem + relevant doc refs so the agent
                # can ask one question and learn *which schematic page*
                # this net lives on, not just what it is electrically.
                for dut in bench.dut_slots:
                    sub = dut.subsystem_for_net(net_name)
                    if sub is not None:
                        payload["dut"] = dut.name
                        payload["subsystem"] = {
                            "name": sub.name,
                            "summary": sub.summary,
                            "doc_refs": [
                                d.model_dump(exclude_none=True) for d in sub.doc_refs
                            ],
                        }
                        if dut.schematic_refs:
                            payload["dut_schematic_refs"] = [
                                d.model_dump(exclude_none=True) for d in dut.schematic_refs
                            ]
                        break
                return json.dumps(payload, indent=2)
        return json.dumps(
            {
                "error": f"Net '{net_name}' not found.",
                "available_nets": [n.name for n in bench.nets],
                "hint": "Call discover_bench() with no argument for the full bench summary.",
            },
            indent=2,
        )

    # The address the agent connected on is the right --box value. Echo it
    # literally when we can see the request; otherwise fall back to a
    # placeholder the agent must fill in itself.
    host = connecting_host()
    box_arg = host or "<box-ip>"

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
        if n.purpose:
            entry["purpose"] = n.purpose
        if n.tags:
            entry["tags"] = n.tags
        nets_out.append(entry)

    summary: dict = {
        "box_id": bench.box_id,
        "hostname": bench.hostname,
        "version": bench.version,
        "run_tests": {
            "note": (
                "This MCP server is read-only — it does not run code or drive "
                "hardware. Author a Python test file using `from lager import "
                "Net, NetType`, then run it from your shell with the lager CLI. "
                "Identify the box by the address you connected to this MCP "
                "server on — local box names are arbitrary client-side aliases, "
                "so this address is the only identifier you can rely on."
            ),
            "box_address": box_arg,
            "command": f"lager python path/to/test.py --box {box_arg}",
            "box_arg": (
                "Pass the box's address (shown in box_address) to --box. No "
                "registration is needed — --box accepts a raw IP directly."
                if host
                else "Pass the box's raw IP to --box. No registration is "
                "needed — --box accepts an IP directly."
            ),
            "reusable_modules": (
                "The runnable can be a single .py file OR a folder. If you pass "
                "a folder, its entrypoint must be `main.py`; the whole folder is "
                "synced and importable, so you can ship reusable helper modules "
                "alongside the test: `lager python path/to/test_dir --box "
                "<box-ip>`."
            ),
            "optional": (
                "Registering a friendly name is optional: "
                "`lager boxes add --name <name> --ip <box-ip>` (only useful for "
                "a stable alias or a non-default SSH user via --user)."
            ),
            "full_docs": "See lager://guide/docs (https://docs.lagerdata.com).",
        },
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
        "instruments": [_instrument_entry(i) for i in bench.instruments],
        "nets": nets_out,
        "interfaces": [
            {"name": i.name, "protocol": i.protocol, "roles": i.roles}
            for i in bench.interfaces
        ],
        "capability_summary": role_counts,
        "total_capabilities": len(graph.nodes),
    }

    # Advisory electrical limits, if the bench authored any. These are NOT
    # enforced by this read-only server — surface them so the test script
    # the agent writes can respect them.
    constraints = getattr(bench, "constraints", None)
    if constraints is not None and (constraints.max_voltage or constraints.max_current):
        summary["advisory_limits"] = {
            "note": (
                "Advisory only — not enforced by this server. Respect these in "
                "the test you write."
            ),
            "max_voltage": constraints.max_voltage,
            "max_current": constraints.max_current,
        }

    if not bench.nets:
        summary["warning"] = (
            "No nets configured on this box. Tests will not be able to address "
            "any hardware until nets are registered. Ask the user to run "
            "'lager nets add-all' on the box to auto-discover connected "
            "instruments (or 'lager nets tui' to add them interactively), then "
            "call discover_bench() again."
        )

    return json.dumps(summary, indent=2)


@mcp.tool()
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
