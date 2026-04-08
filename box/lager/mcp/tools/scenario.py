# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for on-box script execution."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from mcp.server.fastmcp import Context

from ..server import mcp

logger = logging.getLogger(__name__)


@mcp.tool()
async def run_test_script(python_code: str, ctx: Context, timeout: int = 120) -> str:
    """Run a short Python snippet on the box for quick hardware checks.

    Only has access to ``from lager import Net, NetType`` and pip packages.
    No project files, no dtest, no custom modules.

    For real tests, write a test file locally and run it via the shell:
        lager python --serial <BOX> path/to/test.py

    Args:
        python_code: Complete Python source code to execute on-box.
        timeout: Execution timeout in seconds (default: 120).
    """
    from ..engine.scenario_executor import execute_script

    await ctx.report_progress(progress=0, total=2)
    result = execute_script(python_code, timeout_s=timeout)
    await ctx.report_progress(progress=2, total=2)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def install_dependency(package_name: str) -> str:
    """Install a Python package on the box via pip.

    Args:
        package_name: Package name (e.g. "numpy", "crcmod==1.7").
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package_name],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"pip install timed out after 120s"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    return json.dumps({
        "status": "ok" if result.returncode == 0 else "error",
        "exit_code": result.returncode,
        "output": result.stdout.strip(),
        "stderr": result.stderr.strip() if result.stderr.strip() else None,
    }, indent=2)
