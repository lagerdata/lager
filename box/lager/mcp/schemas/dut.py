# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""DUT (Device Under Test) context schemas.

DUTContext gives an AI agent narrative context about *what the box tests*:
the product, MCU, subsystems, and pointers to schematics/datasheets the
agent can ingest with its own multimodal tools. The box itself stays lean
-- it does not store the documents, only references to them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DocKind = Literal[
    "schematic",
    "layout",
    "datasheet",
    "firmware",
    "manual",
    "errata",
    "other",
]


class DocRef(BaseModel):
    """A reference to an external document the agent can fetch and analyse.

    Either ``url`` or ``repo_path`` must be set (or both). ``repo_path`` is
    interpreted relative to the user's test project (the directory synced
    to the box when running ``lager python --serial <BOX> path/to/test.py``),
    so the agent can open it with its local file tools without any blob
    transfer over MCP.
    """

    title: str
    kind: DocKind = "other"
    url: str | None = None
    repo_path: str | None = None
    pages: str | None = None  # e.g. "3", "3-5", "POWER sheet"
    notes: str | None = None


class SubSystem(BaseModel):
    """A logical block of the DUT (e.g. *Power tree*, *Flash subsystem*).

    Lets the agent reason at the level of *systems*, not just individual
    wires. ``nets`` references nets by name; ``doc_refs`` points at the
    relevant schematic sheets / datasheet pages for this subsystem.
    """

    name: str
    summary: str = ""
    nets: list[str] = Field(default_factory=list)
    doc_refs: list[DocRef] = Field(default_factory=list)


class DUTContext(BaseModel):
    """Narrative, system-level context for a DUT slot.

    This replaces the old, anaemic DUTSlot. The legacy ``name``, ``active``,
    ``board_profile``, and ``firmware`` fields are preserved so existing
    bench.json files keep loading; everything else is additive.
    """

    name: str
    active: bool = True
    board_profile: str | None = None
    firmware: str | None = None

    purpose: str = ""
    """One-line: *"power-regression box for FeatureA boards"*."""

    summary: str = ""
    """Markdown paragraph: what the DUT is, what the box tests, known quirks."""

    mcu: str | None = None
    key_peripherals: list[str] = Field(default_factory=list)

    schematic_refs: list[DocRef] = Field(default_factory=list)
    datasheet_refs: list[DocRef] = Field(default_factory=list)
    firmware_refs: list[DocRef] = Field(default_factory=list)
    extra_docs: list[DocRef] = Field(default_factory=list)

    subsystems: list[SubSystem] = Field(default_factory=list)

    def all_doc_refs(self) -> list[DocRef]:
        """Every DocRef attached to this DUT, including subsystems."""
        out: list[DocRef] = []
        out.extend(self.schematic_refs)
        out.extend(self.datasheet_refs)
        out.extend(self.firmware_refs)
        out.extend(self.extra_docs)
        for sub in self.subsystems:
            out.extend(sub.doc_refs)
        return out

    def subsystem_for_net(self, net_name: str) -> SubSystem | None:
        """Return the first SubSystem whose ``nets`` contains ``net_name``."""
        for sub in self.subsystems:
            if net_name in sub.nets:
                return sub
        return None
