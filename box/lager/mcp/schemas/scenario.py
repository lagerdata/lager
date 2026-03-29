# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Scenario schemas for multi-step HIL execution.

v0 scope: flat sequential steps only. The on-box interpreter
(engine/scenario_runner.py) dispatches each ScenarioStep to a
registered action handler. Loop/branch/sweep step types are deferred
until the runner supports them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Scenario step models
# ---------------------------------------------------------------------------

class ScenarioStep(BaseModel):
    """A single action within a scenario."""

    action: str  # "set_voltage", "spi_transfer", "wait", "gpio_set", ...
    target: str | None = None  # net or interface name
    params: dict[str, Any] = Field(default_factory=dict)
    on_failure: str = "abort"  # "abort", "continue", "retry"
    max_retries: int = 0
    timeout_s: int | None = None


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

class Assertion(BaseModel):
    """A named boolean check evaluated after scenario execution."""

    name: str
    expression: str  # Python expression with access to results dict
    severity: str = "error"  # "error", "warning", "info"


# ---------------------------------------------------------------------------
# Top-level scenario
# ---------------------------------------------------------------------------

class Scenario(BaseModel):
    """
    A complete HIL test scenario.

    Executed by the on-box interpreter (engine/scenario_runner.py)
    via the box's Python execution service (:5000/python).

    v0: flat sequential steps only -- matching what the runner
    actually implements.
    """

    name: str
    description: str | None = None
    timeout_s: int = 300
    setup: list[ScenarioStep] = Field(default_factory=list)
    steps: list[ScenarioStep] = Field(default_factory=list)
    cleanup: list[ScenarioStep] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Scenario execution result
# ---------------------------------------------------------------------------

class AssertionResult(BaseModel):
    name: str
    passed: bool
    severity: str = "error"
    detail: str | None = None


class StepResult(BaseModel):
    action: str
    target: str | None = None
    label: str | None = None
    success: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0


class ScenarioResult(BaseModel):
    """Structured result returned after on-box scenario execution."""

    scenario_name: str
    status: str = "unknown"  # "passed", "failed", "error", "timeout", "aborted"
    start_time: datetime = Field(default_factory=_utcnow)
    end_time: datetime | None = None
    duration_ms: float = 0.0
    step_results: list[StepResult] = Field(default_factory=list)
    assertions: list[AssertionResult] = Field(default_factory=list)
    error: str | None = None
    raw_output: str | None = None
