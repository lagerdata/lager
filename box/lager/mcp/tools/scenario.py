# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for coarse-grained scenario execution on-box.

Primary v0 workflow: agent calls run_scenario with structured JSON;
the box executes the full sequence locally and returns results.
run_hil_program exists as an expert/debug escape hatch.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..server import mcp

logger = logging.getLogger(__name__)


def _preflight_scenario(
    scenario_steps: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Validate scenario steps against the bench before uploading to the box.

    Checks:
      - Every step with a ``target`` references a known net
      - Voltage/current params respect bench safety constraints

    Returns an error dict if preflight fails, or None if OK.
    """
    from ..safety import preflight_check
    from ..server_state import get_bench

    bench = get_bench()
    net_names = {n.name for n in bench.nets}
    constraints = bench.constraints

    for i, step in enumerate(scenario_steps):
        target = step.get("target")
        if target and net_names and target not in net_names:
            return {
                "error": (
                    f"Step {i} targets net '{target}' which does not exist "
                    f"on this bench. Known nets: {sorted(net_names)}"
                ),
            }

        params = step.get("params", {})
        action = step.get("action", "")
        if constraints and (params.get("voltage") is not None or params.get("current") is not None):
            result = preflight_check(
                tool_name=action,
                params=params,
                constraints=constraints,
                target_net=target,
            )
            if not result.allowed:
                return {
                    "error": (
                        f"Safety preflight blocked step {i} ({action} on "
                        f"{target}): {result.blocked_reason}"
                    ),
                    "mitigations": result.mitigations,
                }

    return None


@mcp.tool()
def run_scenario(scenario_json: str) -> str:
    """Execute a multi-step HIL scenario on the box.

    The scenario JSON is sent to a fixed on-box interpreter that
    dispatches each step to a registered action handler. Protocol
    interactions, assertions, and GPIO operations all run on-box
    in a single execution with no host round-trips.

    Args:
        scenario_json: JSON string with ``name`` and ``steps``.
            Optional: ``setup``, ``cleanup``, ``assertions``, ``timeout_s``.
            Each step has ``action``, optional ``target`` and ``params``.

    Returns:
        JSON result with status, step results, assertion outcomes,
        and collected results keyed by label.
    """
    from ..schemas.scenario import Scenario
    from ..server_state import get_box_ip
    from ..engine.scenario_executor import execute_scenario_on_box

    try:
        payload = json.loads(scenario_json)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"Invalid JSON: {exc}"})

    try:
        scenario = Scenario(**payload)
    except Exception as exc:
        return json.dumps({"error": f"Invalid scenario schema: {exc}"})

    box_ip = get_box_ip()
    if not box_ip:
        return json.dumps({"error": "No box configured. Set LAGER_BOX or LAGER_BOX_IP."})

    all_steps = (
        [s.model_dump() for s in scenario.setup]
        + [s.model_dump() for s in scenario.steps]
        + [s.model_dump() for s in scenario.cleanup]
    )
    preflight_err = _preflight_scenario(all_steps)
    if preflight_err:
        return json.dumps(preflight_err, indent=2)

    result = execute_scenario_on_box(
        box_ip,
        scenario_json,
        timeout_s=scenario.timeout_s,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def run_hil_program(code: str, timeout: int = 120) -> str:
    """Run a raw Python program on the box via lager python.

    Use this for ad-hoc hardware interaction when the structured
    scenario DSL is too restrictive. The code executes in the box's
    Python container with full access to ``lager.nets``,
    ``lager.protocols.*``, ``lager.io.*``, etc.

    Args:
        code: Python source code to execute on-box.
        timeout: Execution timeout in seconds (default: 120).

    Returns:
        stdout/stderr output from the program.
    """
    from ..server_state import get_box_ip
    from ..engine.scenario_executor import execute_script_on_box

    box_ip = get_box_ip()
    if not box_ip:
        return json.dumps({"error": "No box configured. Set LAGER_BOX or LAGER_BOX_IP."})

    result = execute_script_on_box(box_ip, code, timeout_s=timeout)
    return json.dumps(result, indent=2, default=str)
