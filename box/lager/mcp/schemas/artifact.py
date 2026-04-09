# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Artifact model schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtifactSpec(BaseModel):
    """Declares an artifact to collect during a scenario."""

    type: str  # "log", "waveform", "power_trace", "protocol_transcript"
    source: str  # net name, "stdout", or file path
    format: str | None = None  # "csv", "json", "binary", "text"


class ArtifactRef(BaseModel):
    """Lightweight reference to a stored artifact."""

    id: str
    type: str
    producing_tool: str = ""
    timestamp: datetime = Field(default_factory=_utcnow)


class Artifact(BaseModel):
    """Full artifact record with provenance."""

    id: str
    timestamp: datetime = Field(default_factory=_utcnow)
    box_id: str = ""
    dut_id: str | None = None
    type: str
    producing_tool: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    data_ref: str = ""  # local file path on box
    retention_policy: str = "session"  # "session", "persistent", "ephemeral"


class ArtifactManifest(BaseModel):
    """A collection of artifacts from a single scenario or session."""

    session_id: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)
