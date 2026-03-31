# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Execute scenarios directly on-box via the interpreter runner.

Since the MCP server runs on the box, we import and call the scenario
runner in-process — no HTTP upload, no wire format parsing, no network
overhead. A multi-step scenario executes with sub-millisecond latency
between steps.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)


def execute_scenario(scenario_json: str, *, timeout_s: int = 300) -> dict[str, Any]:
    """
    Execute a scenario in-process using the on-box runner.

    The runner walks setup → steps → cleanup sequentially, dispatching
    each step to hardware via the lager.Net API. All steps execute
    locally with no network round trips.

    Args:
        scenario_json: JSON-serialised scenario.
        timeout_s: Hard wall-clock budget passed to the runner. Overrides
            the ``timeout_s`` field inside the JSON.
    """
    from .scenario_runner import run

    try:
        return run(scenario_json, timeout_s=timeout_s)
    except json.JSONDecodeError as exc:
        return {"status": "error", "error": f"Invalid scenario JSON: {exc}"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def execute_script(
    script: str,
    *,
    timeout_s: int = 300,
    env_vars: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Execute an arbitrary Python script on-box as a subprocess.

    Used by the run_hil_program escape-hatch tool. Runs in a subprocess
    for isolation — a crashing script won't take down the MCP server.
    """
    env = dict(**__import__("os").environ, **(env_vars or {}))

    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": f"Script timed out after {timeout_s}s"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    parsed: dict[str, Any] = {}
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    out: dict[str, Any] = {
        "status": parsed.get("status", "ok" if result.returncode == 0 else "error"),
        "exit_code": result.returncode,
        "output": stdout,
    }
    if stderr:
        out["stderr"] = stderr
    if parsed:
        out.update(parsed)
    return out
