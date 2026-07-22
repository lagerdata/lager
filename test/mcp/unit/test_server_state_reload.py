# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Tests for mtime-based auto-reload of bench state in server_state.

The MCP server holds the bench/capability-graph in module-level singletons.
When a user edits ``bench.json`` / ``saved_nets.json`` (e.g. via
``lager dut edit``), the server should pick the change up on the next
request without an explicit ``box_manage(action="reload")`` or restart.
"""

import os
import time

import pytest

import lager.mcp.server_state as server_state
from lager.mcp.schemas.bench import BenchDefinition


@pytest.fixture(autouse=True)
def _reset_state():
    """Snapshot and restore module globals so tests don't leak state."""
    saved = (
        server_state._bench,
        server_state._graph,
        server_state._config_mtimes,
        server_state._WATCHED_CONFIG_PATHS,
    )
    yield
    (
        server_state._bench,
        server_state._graph,
        server_state._config_mtimes,
        server_state._WATCHED_CONFIG_PATHS,
    ) = saved


def _bump_mtime(path: str) -> None:
    """Force a future mtime so the change is detectable within the same second."""
    future = time.time() + 10
    os.utime(path, (future, future))


def test_auto_reload_on_file_change(tmp_path, monkeypatch):
    cfg = tmp_path / "bench.json"
    cfg.write_text("v1")
    monkeypatch.setattr(server_state, "_WATCHED_CONFIG_PATHS", (str(cfg),))

    def fake_load_from_files():
        return BenchDefinition(box_id=cfg.read_text())

    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files", fake_load_from_files
    )

    server_state.init_state()
    assert server_state.get_bench().box_id == "v1"

    cfg.write_text("v2")
    _bump_mtime(str(cfg))

    # Next access should transparently reload.
    assert server_state.get_bench().box_id == "v2"


def test_no_reload_when_unchanged(tmp_path, monkeypatch):
    cfg = tmp_path / "bench.json"
    cfg.write_text("stable")
    monkeypatch.setattr(server_state, "_WATCHED_CONFIG_PATHS", (str(cfg),))

    calls = {"n": 0}

    def fake_load_from_files():
        calls["n"] += 1
        return BenchDefinition(box_id="stable")

    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files", fake_load_from_files
    )

    server_state.init_state()
    assert calls["n"] == 1
    server_state.get_bench()
    server_state.get_bench()
    # No file change → no extra reload.
    assert calls["n"] == 1


def test_injected_state_never_auto_reloads(tmp_path, monkeypatch):
    cfg = tmp_path / "bench.json"
    cfg.write_text("ondisk")
    monkeypatch.setattr(server_state, "_WATCHED_CONFIG_PATHS", (str(cfg),))

    # Injected bench should disable file watching entirely.
    server_state.init_state(bench=BenchDefinition(box_id="injected"))
    assert server_state._config_mtimes == {}

    cfg.write_text("changed")
    _bump_mtime(str(cfg))

    assert server_state.get_bench().box_id == "injected"
