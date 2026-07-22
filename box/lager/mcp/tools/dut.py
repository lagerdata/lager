# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for DUT-level orientation.

These tools give an agent a quick way to ask *"what is this box / DUT?"*
and *"which document do I look at for this net?"* without scraping the
full netlist or downloading any PDFs.
"""

from __future__ import annotations

import json

from ..server import connecting_host, mcp
from ..server_state import get_bench


def _find_dut_for_net(bench, net_name: str):
    """Return (dut, subsystem) for the DUT/subsystem owning ``net_name``.

    If no subsystem references the net, returns ``(primary_dut, None)`` so
    callers can still surface DUT-level docs.
    """
    for dut in bench.dut_slots:
        sub = dut.subsystem_for_net(net_name)
        if sub is not None:
            return dut, sub
    return bench.primary_dut(), None


@mcp.tool()
def discover_dut() -> str:
    """Quick orientation: what is this box and what does it test?

    Returns the DUT purpose, MCU, key peripherals, subsystem list, and
    pointers to the schematic / datasheet / firmware references. The actual
    documents are NOT included in the response — use your own file tools
    to open ``repo_path`` entries or fetch ``url`` entries.

    For the full structured shape, read the ``lager://dut/context``
    resource. For a markdown briefing, read ``lager://dut/overview.md``.
    """
    bench = get_bench()
    duts = bench.dut_slots
    host = connecting_host()
    box_arg = host or "<box-ip>"

    if not duts:
        return json.dumps({
            "box_id": bench.box_id,
            "dut_slots": [],
            "warning": (
                "No DUT context has been authored on this box yet. "
                "Ask the user to run 'lager dut edit' to describe what "
                "this box tests, the DUT MCU and peripherals, and to attach "
                "references to the schematic / datasheets."
            ),
        }, indent=2)

    out = {
        "box_id": bench.box_id,
        "hostname": bench.hostname,
        "calibration_healthy": bench.calibration.healthy if bench.calibration else True,
        "dut_slots": [],
        "next_steps": [
            "Read lager://dut/overview.md for a full narrative briefing.",
            "Call discover_bench() to enumerate hardware.",
            "Call cite_schematic(<net>) to pinpoint the doc page for a specific net.",
            "When your test is written, run it from your shell with the box's "
            "address (the same address you connected to this MCP server on): "
            f"lager python path/to/test.py --box {box_arg}",
        ],
    }

    for dut in duts:
        out["dut_slots"].append({
            "name": dut.name,
            "active": dut.active,
            "purpose": dut.purpose,
            "summary": dut.summary,
            "board_profile": dut.board_profile,
            "mcu": dut.mcu,
            "firmware": dut.firmware,
            "key_peripherals": dut.key_peripherals,
            "subsystems": [
                {
                    "name": s.name,
                    "summary": s.summary,
                    "nets": s.nets,
                    "doc_refs": [d.model_dump(exclude_none=True) for d in s.doc_refs],
                }
                for s in dut.subsystems
            ],
            "schematic_refs": [d.model_dump(exclude_none=True) for d in dut.schematic_refs],
            "datasheet_refs": [d.model_dump(exclude_none=True) for d in dut.datasheet_refs],
            "firmware_refs": [d.model_dump(exclude_none=True) for d in dut.firmware_refs],
            "extra_docs": [d.model_dump(exclude_none=True) for d in dut.extra_docs],
        })

    return json.dumps(out, indent=2)


@mcp.tool()
def cite_schematic(net_name: str) -> str:
    """Return the doc references relevant to a specific net.

    Pinpoints the schematic / datasheet pages an agent should open when
    reasoning about a single net, instead of asking it to scan the whole
    PDF.  Includes the DUT-level schematic refs plus any refs attached
    to the subsystem that owns the net.

    Args:
        net_name: The net to look up (e.g. "uart1", "spi_flash_cs").
    """
    bench = get_bench()

    net = next((n for n in bench.nets if n.name == net_name), None)
    if net is None:
        return json.dumps({"error": f"Net '{net_name}' not found."})

    dut, sub = _find_dut_for_net(bench, net_name)
    if dut is None:
        return json.dumps({
            "net": net_name,
            "warning": "No DUT context has been authored on this box yet.",
            "doc_refs": [],
        })

    schematic_refs = [d.model_dump(exclude_none=True) for d in dut.schematic_refs]
    datasheet_refs = [d.model_dump(exclude_none=True) for d in dut.datasheet_refs]
    subsystem_refs: list[dict] = []
    if sub is not None:
        subsystem_refs = [d.model_dump(exclude_none=True) for d in sub.doc_refs]

    return json.dumps({
        "net": net_name,
        "net_purpose": net.purpose,
        "net_notes": net.notes,
        "dut": dut.name,
        "subsystem": sub.name if sub is not None else None,
        "subsystem_summary": sub.summary if sub is not None else None,
        "schematic_refs": schematic_refs,
        "datasheet_refs": datasheet_refs,
        "subsystem_doc_refs": subsystem_refs,
        "guidance": (
            "Open `repo_path` entries with your own file tools, or fetch "
            "`url` entries directly. Prefer per-sheet PNG exports for "
            "vision analysis; use the `pages` field to focus on the "
            "relevant sheet."
        ),
    }, indent=2)
