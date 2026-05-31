# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures, marks, and config for MCP server tests."""

import sys
from pathlib import Path

_box_dir = str(Path(__file__).resolve().parents[2] / "box")
if _box_dir not in sys.path:
    sys.path.insert(0, _box_dir)

import pytest


# --- Marks ---
def pytest_configure(config):
    config.addinivalue_line("markers", "unit: unit tests")
    config.addinivalue_line("markers", "integration: live integration tests")
