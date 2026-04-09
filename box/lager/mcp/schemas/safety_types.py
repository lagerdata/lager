# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Safety constraint and preflight result schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RateLimit(BaseModel):
    """Rate-limiting config for a tool or action."""

    max_calls: int = 60
    window_seconds: int = 60


class SafetyConstraints(BaseModel):
    """
    Per-bench safety configuration.

    Loaded from bench.json or provided as defaults. Applied by the
    preflight engine before every tool invocation and scenario step.
    """

    max_voltage: dict[str, float] = Field(default_factory=dict)
    max_current: dict[str, float] = Field(default_factory=dict)
    dangerous_actions: list[str] = Field(default_factory=list)
    rate_limits: dict[str, RateLimit] = Field(default_factory=dict)
    destructive_mode: bool = False


class PreflightResult(BaseModel):
    """Outcome of a safety preflight check."""

    allowed: bool = True
    warnings: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    mitigations: list[str] = Field(default_factory=list)
