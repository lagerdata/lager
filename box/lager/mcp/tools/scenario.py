# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
MCP tools for on-box script execution.

run_test_script is the PRIMARY execution tool.  The agent writes a Python
script using ``from lager import Net, NetType`` and this tool runs it
directly on the box.  All hardware calls are local — one round trip total.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any

from mcp.server.fastmcp import Context

from ..server import mcp

logger = logging.getLogger(__name__)


@mcp.tool()
async def run_test_script(python_code: str, ctx: Context, timeout: int = 120) -> str:
    """Run a Python test script on the box and return structured results.

    This is the primary way to execute hardware-in-the-loop tests.  Write
    a complete Python script using the ``lager`` API (``from lager import
    Net, NetType``), and this tool runs it on the box as a subprocess.

    The script has full access to:
      - ``from lager import Net, NetType`` — all hardware via named nets
      - Standard library and any pip-installed packages
      - stdout/stderr for output

    **Tip:** Print a JSON object on the last line of stdout for structured
    results — it will be parsed into the response automatically.

    Args:
        python_code: Complete Python source code to execute on-box.
        timeout: Execution timeout in seconds (default: 120).

    Returns:
        JSON with status, exit_code, stdout output, stderr (if any),
        and duration_s.  If the last stdout line is valid JSON, its
        fields are merged into the response.
    """
    from ..engine.scenario_executor import execute_script

    await ctx.report_progress(progress=0, total=2)
    result = execute_script(python_code, timeout_s=timeout)
    await ctx.report_progress(progress=2, total=2)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def install_dependency(package_name: str) -> str:
    """Install a Python package on the box via pip.

    Use this if your test script needs a package that isn't pre-installed
    (e.g., numpy, pyyaml, crcmod).  Standard packages (serial, json, time,
    struct, etc.) and the ``lager`` API are always available.

    Args:
        package_name: Package name (e.g., "numpy", "crcmod==1.7").
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
