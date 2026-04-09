# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for running Python scripts on Lager boxes."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_python_run(
    box: str, script_path: str,
    timeout: int = 60, detach: bool = False,
) -> str:
    """Upload and run a Python script on a Lager box.

    The script is uploaded to the box and executed. Use detach=True
    to run the script in the background.

    Args:
        box: Box name (e.g., 'DEMO')
        script_path: Local path to the Python script to upload and run
        timeout: Script execution timeout in seconds (default: 60)
        detach: Run script in background (default: false)
    """
    args = ["python", script_path, "--box", box]
    if timeout != 60:
        args.extend(["--timeout", str(timeout)])
    if detach:
        args.append("--detach")
    return run_lager(*args, timeout=max(timeout + 10, 120))


@mcp.tool()
def lager_python_kill(box: str, signal: str = "SIGTERM") -> str:
    """Kill a running Python script on a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
        signal: Signal to send (default: 'SIGTERM')
    """
    args = ["python", "--kill", "--box", box]
    if signal != "SIGTERM":
        args.extend(["--signal", signal])
    return run_lager(*args)
