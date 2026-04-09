# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Safety preflight engine.

Runs before every tool invocation and every scenario step to enforce
voltage/current limits, rate limits, and dangerous-action guardrails.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from .schemas.safety_types import PreflightResult, SafetyConstraints

logger = logging.getLogger(__name__)

# Per-tool invocation timestamps for rate limiting
_call_history: dict[str, list[float]] = defaultdict(list)


def preflight_check(
    *,
    tool_name: str,
    params: dict[str, Any],
    constraints: SafetyConstraints | None,
    target_net: str | None = None,
) -> PreflightResult:
    """
    Run safety preflight before a tool invocation.

    Returns a PreflightResult indicating whether the action is allowed
    and any warnings or mitigations.
    """
    warnings: list[str] = []
    mitigations: list[str] = []

    if constraints is None:
        return PreflightResult(allowed=True)

    # --- Voltage limits ---
    voltage = params.get("voltage")
    if voltage is not None and target_net:
        max_v = constraints.max_voltage.get(target_net)
        if max_v is not None and voltage > max_v:
            return PreflightResult(
                allowed=False,
                blocked_reason=f"Requested voltage {voltage}V exceeds max {max_v}V for net '{target_net}'.",
                mitigations=[f"Reduce voltage to <= {max_v}V."],
            )
        if max_v is not None and voltage > max_v * 0.9:
            warnings.append(f"Voltage {voltage}V is within 10% of max {max_v}V for net '{target_net}'.")

    # --- Current limits ---
    current = params.get("current") or params.get("current_limit")
    if current is not None and target_net:
        max_i = constraints.max_current.get(target_net)
        if max_i is not None and current > max_i:
            return PreflightResult(
                allowed=False,
                blocked_reason=f"Requested current {current}A exceeds max {max_i}A for net '{target_net}'.",
                mitigations=[f"Reduce current to <= {max_i}A."],
            )

    # --- Dangerous actions ---
    if tool_name in constraints.dangerous_actions and not constraints.destructive_mode:
        return PreflightResult(
            allowed=False,
            blocked_reason=f"Tool '{tool_name}' is listed as a dangerous action. Enable destructive_mode to proceed.",
            mitigations=["Set destructive_mode=true in bench safety constraints."],
        )

    # --- Rate limits ---
    rl = constraints.rate_limits.get(tool_name)
    if rl:
        now = time.monotonic()
        history = _call_history[tool_name]
        cutoff = now - rl.window_seconds
        _call_history[tool_name] = [t for t in history if t > cutoff]
        if len(_call_history[tool_name]) >= rl.max_calls:
            return PreflightResult(
                allowed=False,
                blocked_reason=f"Rate limit exceeded for '{tool_name}': {rl.max_calls} calls per {rl.window_seconds}s.",
                mitigations=[f"Wait before retrying."],
            )
        _call_history[tool_name].append(now)

    return PreflightResult(allowed=True, warnings=warnings, mitigations=mitigations)


def reset_rate_limits() -> None:
    """Clear rate limit history (for testing)."""
    _call_history.clear()
