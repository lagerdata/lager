# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""MCP resources for DUT-level context.

Two resources are exposed:

- ``lager://dut/context`` -- raw JSON of every DUTContext on the bench
  (purpose, summary, MCU, subsystems, doc refs).
- ``lager://dut/overview.md`` -- a generated markdown briefing the agent
  should read first: what the box tests, the DUT block diagram, the
  schematics/datasheets to fetch, and a table of nets grouped by
  subsystem with their ``purpose``.
"""

from __future__ import annotations

import json

from ..server_state import get_bench


def _doc_ref_md(ref) -> str:
    """Render a single DocRef as a markdown list item."""
    parts: list[str] = [f"**{ref.title}**"]
    where: list[str] = []
    if ref.url:
        where.append(f"[{ref.url}]({ref.url})")
    if ref.repo_path:
        where.append(f"`{ref.repo_path}` (in your project repo)")
    if where:
        parts.append(" — " + " / ".join(where))
    if ref.pages:
        parts.append(f"  \n  pages: {ref.pages}")
    if ref.notes:
        parts.append(f"  \n  {ref.notes}")
    return "- " + "".join(parts)


def _render_overview(bench) -> str:
    lines: list[str] = ["# DUT Overview\n"]

    if bench.box_id:
        lines.append(f"**Box:** `{bench.box_id}`")
        if bench.hostname:
            lines[-1] += f" ({bench.hostname})"
        lines.append("")

    if not bench.dut_slots:
        lines.append(
            "_No DUT context has been authored on this box yet._\n\n"
            "Ask the user to run `lager box dut edit` to describe what this "
            "box tests, the DUT MCU/peripherals, and to attach references "
            "to the schematic / datasheets."
        )
        return "\n".join(lines)

    for dut in bench.dut_slots:
        header = f"## DUT: {dut.name}"
        if not dut.active:
            header += "  _(inactive)_"
        lines.append(header)

        if dut.purpose:
            lines.append(f"\n**Purpose:** {dut.purpose}")
        if dut.board_profile:
            lines.append(f"**Board profile:** `{dut.board_profile}`")
        if dut.mcu:
            lines.append(f"**MCU:** `{dut.mcu}`")
        if dut.firmware:
            lines.append(f"**Firmware:** `{dut.firmware}`")
        if dut.key_peripherals:
            lines.append(
                "**Key peripherals:** " + ", ".join(f"`{p}`" for p in dut.key_peripherals)
            )

        if dut.summary:
            lines.append("\n### Summary\n")
            lines.append(dut.summary)

        # ── Doc references ────────────────────────────────────────────
        doc_sections = [
            ("Schematics", dut.schematic_refs),
            ("Datasheets", dut.datasheet_refs),
            ("Firmware references", dut.firmware_refs),
            ("Other documents", dut.extra_docs),
        ]
        any_docs = any(refs for _, refs in doc_sections)
        if any_docs:
            lines.append("\n### Documents to read")
            lines.append(
                "_The box does not host these files. Use your own file "
                "tools to open `repo_path` entries, or fetch `url` entries "
                "directly. Vision models analyse per-sheet PNGs faster "
                "than full PDFs._\n"
            )
            for title, refs in doc_sections:
                if not refs:
                    continue
                lines.append(f"**{title}**")
                for ref in refs:
                    lines.append(_doc_ref_md(ref))
                lines.append("")

        # ── Subsystems and their nets ────────────────────────────────
        if dut.subsystems:
            lines.append("### Subsystems\n")
            for sub in dut.subsystems:
                lines.append(f"#### {sub.name}")
                if sub.summary:
                    lines.append(sub.summary)
                if sub.nets:
                    nets_in_sub = [n for n in bench.nets if n.name in sub.nets]
                    if nets_in_sub:
                        lines.append("\n| Net | Type | Purpose |")
                        lines.append("| --- | --- | --- |")
                        for n in nets_in_sub:
                            purpose = (n.purpose or "").replace("|", "\\|")
                            lines.append(f"| `{n.name}` | {n.net_type} | {purpose} |")
                if sub.doc_refs:
                    lines.append("\n_Subsystem documents:_")
                    for ref in sub.doc_refs:
                        lines.append(_doc_ref_md(ref))
                lines.append("")

        # ── Orphan nets (not in any subsystem) ───────────────────────
        assigned: set[str] = set()
        for sub in dut.subsystems:
            assigned.update(sub.nets)
        orphan_nets = [n for n in bench.nets if n.name not in assigned]
        if orphan_nets:
            lines.append("### Other nets\n")
            lines.append("| Net | Type | Purpose |")
            lines.append("| --- | --- | --- |")
            for n in orphan_nets:
                purpose = (n.purpose or "").replace("|", "\\|")
                lines.append(f"| `{n.name}` | {n.net_type} | {purpose} |")
            lines.append("")

    return "\n".join(lines)


def register(mcp):
    @mcp.resource("lager://dut/context")
    def dut_context() -> str:
        """Structured JSON of every DUTContext: purpose, MCU, subsystems, doc refs."""
        bench = get_bench()
        payload = {
            "box_id": bench.box_id,
            "hostname": bench.hostname,
            "version": bench.version,
            "dut_slots": [
                d.model_dump(exclude_none=True) for d in bench.dut_slots
            ],
        }
        return json.dumps(payload, indent=2)

    @mcp.resource("lager://dut/overview.md")
    def dut_overview_md() -> str:
        """Markdown briefing: what the box tests + DUT block diagram + doc refs.

        This is the single page an agent should read first to understand
        the system under test.
        """
        return _render_overview(get_bench())
