# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Execute a scenario on a Lager box via the interpreter runner.

Uploads the fixed scenario_runner.py script to POST :5000/python and
passes the scenario JSON as the LAGER_SCENARIO environment variable.
Parses the streaming wire format output and extracts the structured
JSON result from stdout.

v0 simplification: the runner script is re-uploaded on every invocation.
This is simple and correct but adds ~1-2 KB of overhead per call.  A
future version may pre-install the runner on the box filesystem or run
it as a persistent service, eliminating the upload.
"""

from __future__ import annotations

import io
import json
import logging
import pathlib
from typing import Any

logger = logging.getLogger(__name__)

_RUNNER_PATH = pathlib.Path(__file__).parent / "scenario_runner.py"


def _get_runner_source() -> str:
    """Read the fixed scenario runner script."""
    return _RUNNER_PATH.read_text(encoding="utf-8")


def execute_scenario_on_box(
    box_ip: str,
    scenario_json: str,
    *,
    timeout_s: int = 300,
) -> dict[str, Any]:
    """
    Upload the interpreter runner and execute a scenario on the box.

    Args:
        box_ip: IP address of the target Lager box.
        scenario_json: Serialized scenario JSON string.
        timeout_s: Maximum execution time in seconds.

    Returns:
        Structured result dict with at minimum {"status": ...}.
    """
    import requests

    runner_source = _get_runner_source()
    base_url = f"http://{box_ip}:5000"

    script_bytes = runner_source.encode("utf-8")
    files = [
        ("script", ("scenario_runner.py", io.BytesIO(script_bytes), "text/x-python")),
        ("env", (None, f"LAGER_SCENARIO={scenario_json}")),
        ("timeout", (None, str(timeout_s))),
    ]

    try:
        resp = requests.post(
            f"{base_url}/python",
            files=files,
            stream=True,
            timeout=(10, timeout_s + 30),
        )
    except requests.exceptions.ConnectionError as exc:
        return {"status": "error", "error": f"Cannot connect to box at {box_ip}: {exc}", "output": ""}
    except requests.exceptions.Timeout:
        return {"status": "timeout", "error": f"Script execution timed out after {timeout_s}s", "output": ""}

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    try:
        for chunk in resp.iter_content(chunk_size=4096):
            if not chunk:
                continue
            _parse_wire_chunk(chunk, stdout_chunks, stderr_chunks)
    except Exception as exc:
        logger.warning("Error reading stream: %s", exc)

    full_stdout = "".join(stdout_chunks)
    full_stderr = "".join(stderr_chunks)

    parsed: dict[str, Any] = {}
    for line in reversed(full_stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    result: dict[str, Any] = {
        "status": parsed.get("status", "unknown"),
        "output": full_stdout,
    }
    if full_stderr:
        result["stderr"] = full_stderr
    if parsed:
        result.update(parsed)
    return result


def execute_script_on_box(
    box_ip: str,
    script: str,
    *,
    timeout_s: int = 300,
    env_vars: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Upload and execute an arbitrary Python script on the box (escape hatch).

    Used by run_hil_program for raw Python execution.
    """
    import requests

    base_url = f"http://{box_ip}:5000"

    script_bytes = script.encode("utf-8")
    files: list[tuple] = [("script", ("program.py", io.BytesIO(script_bytes), "text/x-python"))]

    env_list = [f"{k}={v}" for k, v in (env_vars or {}).items()]
    for ev in env_list:
        files.append(("env", (None, ev)))

    files.append(("timeout", (None, str(timeout_s))))

    try:
        resp = requests.post(
            f"{base_url}/python",
            files=files,
            stream=True,
            timeout=(10, timeout_s + 30),
        )
    except requests.exceptions.ConnectionError as exc:
        return {"status": "error", "error": f"Cannot connect to box at {box_ip}: {exc}", "output": ""}
    except requests.exceptions.Timeout:
        return {"status": "timeout", "error": f"Script execution timed out after {timeout_s}s", "output": ""}

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    try:
        for chunk in resp.iter_content(chunk_size=4096):
            if not chunk:
                continue
            _parse_wire_chunk(chunk, stdout_chunks, stderr_chunks)
    except Exception as exc:
        logger.warning("Error reading stream: %s", exc)

    full_stdout = "".join(stdout_chunks)
    full_stderr = "".join(stderr_chunks)

    parsed: dict[str, Any] = {}
    for line in reversed(full_stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    result: dict[str, Any] = {
        "status": parsed.get("status", "unknown"),
        "output": full_stdout,
    }
    if full_stderr:
        result["stderr"] = full_stderr
    if parsed:
        result.update(parsed)
    return result


def _parse_wire_chunk(
    chunk: bytes,
    stdout: list[str],
    stderr: list[str],
) -> None:
    """
    Parse the Lager Python service wire format.

    Format: ``<fileno> <length> <data>``
    fileno 0 = keepalive, 1 = stdout, 2 = stderr, 3 = output_channel
    Final: ``- <len> <returncode>``
    """
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        return

    for line in text.splitlines(keepends=True):
        parts = line.split(" ", 2)
        if len(parts) < 2:
            continue
        fno = parts[0]
        if fno == "1" and len(parts) >= 3:
            stdout.append(parts[2])
        elif fno == "2" and len(parts) >= 3:
            stderr.append(parts[2])
        elif fno == "-":
            pass
