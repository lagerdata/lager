# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures, marks, and config for MCP server tests."""

import sys
from pathlib import Path

_box_dir = str(Path(__file__).resolve().parents[2] / "box")
if _box_dir not in sys.path:
    sys.path.insert(0, _box_dir)

import pytest


# --- SDK version guard ---
#
# `lager.mcp.server` imports `mcp.server.mcpserver`, which exists only in MCP
# SDK 2.x -- 2.0 renamed `mcp.server.fastmcp`, and the server was ported to
# the new API, so the floor is a requirement and not just a widened ceiling.
#
# Against an older SDK every test in this tree fails at collection with
# `ModuleNotFoundError: No module named 'mcp.server.mcpserver'`, and not one
# of those failures names the cause. `tools/check_coverage_counts.py` then
# reports the suite as failed and stops before rewriting any count, so a
# developer with a stale environment sees a wall of unrelated breakage.
#
# One message instead, before collection.
#
# Called from `pytest_configure`, not at import: `pytest.exit` raised while
# the conftest module is still loading is reported as "ImportError while
# loading conftest" with a traceback around it, which buries the sentence it
# exists to show.
def _require_mcp_2():
    try:
        from importlib.metadata import version
        installed = version("mcp")
    except Exception:  # pylint: disable=broad-except
        # No distribution metadata is not this check's business to diagnose:
        # the import error the tests raise is clearer than a guess here.
        return
    major = installed.split(".", 1)[0]
    if major.isdigit() and int(major) < 2:
        pytest.exit(
            f"test/mcp needs MCP SDK 2.x (found {installed}). "
            f"Run: pip install -r test/requirements-unit.txt",
            returncode=1,
        )


# --- Marks ---
def pytest_configure(config):
    _require_mcp_2()
    config.addinivalue_line("markers", "unit: unit tests")
    config.addinivalue_line("markers", "integration: live integration tests")
