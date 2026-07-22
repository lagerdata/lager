# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Shared singleton state for the on-box Lager MCP server.

Initialized at startup from local files (/etc/lager/).
"""

from __future__ import annotations

import logging
import os

from .schemas.bench import BenchDefinition
from .schemas.capability import CapabilityGraph

logger = logging.getLogger(__name__)

_bench: BenchDefinition | None = None
_graph: CapabilityGraph | None = None

# Config files watched for changes so edits (e.g. ``lager dut edit`` or
# ``lager nets describe``) are picked up automatically on the next request
# without an agent calling ``box_manage`` or a service restart.
_WATCHED_CONFIG_PATHS = (
    "/etc/lager/saved_nets.json",
    "/etc/lager/bench.json",
    "/etc/lager/box_id",
)
# Snapshot of watched-file mtimes taken at the last file-based load. Empty
# when state was injected directly (tests), which disables auto-reload.
_config_mtimes: dict[str, float] = {}


def _snapshot_config_mtimes() -> dict[str, float]:
    """Return {path: mtime} for watched config files that currently exist."""
    snapshot: dict[str, float] = {}
    for path in _WATCHED_CONFIG_PATHS:
        try:
            snapshot[path] = os.path.getmtime(path)
        except OSError:
            # Missing files are tracked by their absence; if one appears or
            # disappears later, the snapshot will differ and trigger a reload.
            continue
    return snapshot


def init_state(
    *,
    bench: BenchDefinition | None = None,
    graph: CapabilityGraph | None = None,
) -> None:
    """Bootstrap server state from on-box config files."""
    global _bench, _graph, _config_mtimes

    if bench is not None:
        _bench = bench
        # State was injected directly; nothing on disk to watch.
        _config_mtimes = {}
    else:
        from .engine.bench_loader import load_from_files
        # Capture mtimes *before* the read so a write that races the load is
        # caught on the next request rather than being missed.
        _config_mtimes = _snapshot_config_mtimes()
        try:
            _bench = load_from_files()
        except Exception as exc:
            logger.warning("Failed to load bench from local files: %s", exc)
            from .config import get_box_id
            _bench = BenchDefinition(box_id=get_box_id())

    if graph is not None:
        _graph = graph
    else:
        from .engine.capability_graph import build_capability_graph
        _graph = build_capability_graph(_bench)

    logger.info(
        "Bench loaded: box_id=%s, %d nets, %d instruments, %d capabilities",
        _bench.box_id,
        len(_bench.nets),
        len(_bench.instruments),
        len(_graph.nodes),
    )


def _maybe_reload() -> None:
    """Reload bench state if any watched config file changed on disk.

    No-op when state was injected directly (``_config_mtimes`` empty) so
    tests and in-memory benches are never clobbered.
    """
    if not _config_mtimes:
        return
    if _snapshot_config_mtimes() != _config_mtimes:
        logger.info("Config change detected on disk; reloading bench state.")
        init_state()


def get_bench() -> BenchDefinition:
    _maybe_reload()
    if _bench is None:
        return BenchDefinition()
    return _bench


def get_capability_graph() -> CapabilityGraph:
    _maybe_reload()
    if _graph is None:
        return CapabilityGraph()
    return _graph


def reload_bench() -> None:
    """Re-read bench data from local files."""
    init_state()
