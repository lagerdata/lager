# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""On-box script execution engine for MCP tools."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)


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
