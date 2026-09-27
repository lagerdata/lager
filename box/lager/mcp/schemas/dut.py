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

from pydantic import BaseModel, ConfigDict, Field, model_validator

DocKind = Literal[
    "schematic",
    "layout",
    "datasheet",
    "firmware",
    "manual",
    "errata",
    "other",
]

#: Where an agent looks for a ``repo_path`` document that the user's project
#: does not contain. Deliberately outside the project tree: ``lager python``
#: zips the project directory, following symlinks, under a size cap, so a
#: documents folder inside the project would be uploaded on every run.
LOCAL_DOCS_DIR = "~/.lager_dut_docs"


class DocRef(BaseModel):
    """A reference to an external document the agent can fetch and analyse.

    At least one locator must be set: ``url``, ``repo_path``, ``external_id``
    or ``external_url``. ``repo_path`` is interpreted relative to the user's
    test project (the directory synced to the box when running
    ``lager python path/to/test.py --box <box-ip>``); when the project does
    not have the file, the agent looks under ``LOCAL_DOCS_DIR`` at the same
    relative path. ``external_id`` and ``external_url`` name the document in
    an external document store, which the agent reaches through its own
    authenticated connector -- the box never authenticates.

    Unknown fields are kept (``extra="allow"``), so a field written by a newer
    tool survives the trip to the agent.
    """

    model_config = ConfigDict(extra="allow")

    title: str
    kind: DocKind = "other"
    url: str | None = None
    repo_path: str | None = None
    external_id: str | None = None
    external_url: str | None = None
    pages: str | None = None  # e.g. "3", "3-5", "POWER sheet"
    notes: str | None = None

    @model_validator(mode="after")
    def _needs_a_locator(self) -> DocRef:
        if not (self.url or self.repo_path or self.external_id or self.external_url):
            raise ValueError(
                "DocRef needs at least one of url, repo_path, external_id "
                "or external_url"
            )
        return self


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
