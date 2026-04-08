# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for on-box package management."""

from __future__ import annotations

import json
import subprocess
import sys

from ..server import mcp


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
