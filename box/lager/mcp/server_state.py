# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Shared singleton state for the on-box Lager MCP server.

Initialized at startup from local files (/etc/lager/). The :9000 box HTTP
server uses the same module for ``GET /bench``, so each process that needs
the bench holds its own copy and reloads it on its own.

The bench and its capability graph are published as ONE immutable object.
MCP SDK 2.0 runs synchronous tool handlers on worker threads, so a reload
can run while another request reads; with two separate globals a reader
could see the new bench with the old graph. One assignment of one object
rules that out.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

from .schemas.bench import BenchDefinition
from .schemas.capability import CapabilityGraph

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _State:
    bench: BenchDefinition
    graph: CapabilityGraph
    #: True when ``bench`` came from the files on disk. Enables auto-reload
    #: and the live instrument inventory; injected state (tests) gets neither.
    file_backed: bool = False
    #: Watched-file mtimes at the time of the load; see ``_maybe_reload``.
    config_mtimes: dict[str, float] = field(default_factory=dict)


_state: _State | None = None

# Config files watched for changes so edits (e.g. ``lager dut edit`` or
# ``lager nets describe``) are picked up automatically on the next request
# without an agent calling ``box_manage`` or a service restart.
_WATCHED_CONFIG_PATHS = (
    "/etc/lager/saved_nets.json",
    "/etc/lager/bench.json",
    "/etc/lager/box_id",
)

# Serialises reloads. Two requests can notice the same mtime change at once;
# the second re-checks under the lock and finds the first already did the
# work.
_reload_lock = threading.Lock()


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
    """Bootstrap server state from on-box config files, or from ``bench``."""
    global _state

    file_backed = bench is None
    mtimes: dict[str, float] = {}
    if bench is None:
        from .engine.bench_loader import load_from_files
        # Capture mtimes *before* the read so a write that races the load is
        # caught on the next request rather than being missed.
        mtimes = _snapshot_config_mtimes()
        try:
            bench = load_from_files()
        except Exception as exc:
            logger.warning("Failed to load bench from local files: %s", exc)
            from .config import get_box_id
            bench = BenchDefinition(box_id=get_box_id())

    if graph is None:
        from .engine.capability_graph import build_capability_graph
        graph = build_capability_graph(bench)

    # One assignment publishes bench and graph together.
    _state = _State(
        bench=bench, graph=graph, file_backed=file_backed, config_mtimes=mtimes,
    )

    logger.info(
        "Bench loaded: box_id=%s, %d nets, %d capabilities%s",
        bench.box_id,
        len(bench.nets),
        len(graph.nodes),
        "" if file_backed else " (injected)",
    )


def _maybe_reload() -> None:
    """Reload bench state if any watched config file changed on disk.

    No-op when state was injected directly so tests and in-memory benches
    are never clobbered.
    """
    state = _state
    if state is None or not state.file_backed:
        return
    if _snapshot_config_mtimes() == state.config_mtimes:
        return
    with _reload_lock:
        # Re-check under the lock: a thread that queued behind the reload
        # would otherwise repeat work the winner already did.
        state = _state
        if state is None or not state.file_backed:
            return
        if _snapshot_config_mtimes() == state.config_mtimes:
            return
        logger.info("Config change detected on disk; reloading bench state.")
        init_state()


def _current() -> _State | None:
    _maybe_reload()
    return _state


def _bench_of(state: _State) -> BenchDefinition:
    """The state's bench, with the live instrument inventory when file-backed.

    Instruments are attached on a shallow copy so the shared state is never
    mutated; see ``engine.instruments`` for the scan cache behind them.
    """
    if not state.file_backed:
        return state.bench
    from .engine.instruments import cached_instruments
    return state.bench.model_copy(update={"instruments": cached_instruments()})


def get_bench_and_graph() -> tuple[BenchDefinition, CapabilityGraph]:
    """The bench and the graph built from it, read from ONE published state.

    A caller that needs both must use this: two separate ``get_bench()`` and
    ``get_capability_graph()`` calls can straddle a reload and pair the new
    bench with the old graph.
    """
    state = _current()
    if state is None:
        return BenchDefinition(), CapabilityGraph()
    return _bench_of(state), state.graph


def get_bench() -> BenchDefinition:
    state = _current()
    if state is None:
        return BenchDefinition()
    return _bench_of(state)


def get_capability_graph() -> CapabilityGraph:
    state = _current()
    if state is None:
        return CapabilityGraph()
    return state.graph


def reload_bench() -> None:
    """Re-read bench data from local files, under the same lock as auto-reload."""
    with _reload_lock:
        init_state()


def ensure_loaded() -> None:
    """Load state from disk on first use, for hosts that do not call ``init_state``
    at startup (the :9000 box HTTP server)."""
    if _state is not None:
        return
    with _reload_lock:
        if _state is None:
            init_state()
