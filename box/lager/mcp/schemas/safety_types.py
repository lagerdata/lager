# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Advisory safety-limit schema.

These limits are **advisory only**. The MCP server is read-only and does
not drive hardware, so it cannot enforce anything — it surfaces these
limits to the agent (via ``discover_bench``) so the test script the agent
writes can respect them. Actual enforcement, if any, belongs in the test
code or the instrument configuration.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SafetyConstraints(BaseModel):
    """Per-bench advisory electrical limits, keyed by net name.

    Loaded from the ``constraints`` block of bench.json and surfaced to the
    agent as guidance. Unknown keys are ignored, so older bench.json files
    that carried enforcement-era fields still load cleanly.
    """

    max_voltage: dict[str, float] = Field(default_factory=dict)
    max_current: dict[str, float] = Field(default_factory=dict)
