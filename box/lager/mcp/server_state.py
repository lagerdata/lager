# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Shared singleton state for the Lager MCP server.

Holds the loaded BenchDefinition, CapabilityGraph, and box connection
info. Initialized once at server startup and read by resources, tools,
and engines.
"""

from __future__ import annotations

import logging
from typing import Any

from .schemas.bench import BenchDefinition
from .schemas.capability import CapabilityGraph

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_bench: BenchDefinition | None = None
_graph: CapabilityGraph | None = None
_box_ip: str = ""


def init_state(
    *,
    box_ip: str = "",
    bench: BenchDefinition | None = None,
    graph: CapabilityGraph | None = None,
) -> None:
    """
    Bootstrap server state.

    If *bench* is not provided, attempts to load from the live box via
    HTTP using *box_ip*.
    """
    global _bench, _graph, _box_ip
    _box_ip = box_ip

    if bench is not None:
        _bench = bench
    elif box_ip:
        from .engine.bench_loader import load_from_box
        try:
            _bench = load_from_box(box_ip)
        except Exception as exc:
            logger.warning("Failed to load bench from %s: %s -- using empty bench", box_ip, exc)
            _bench = BenchDefinition(box_id=box_ip)
    else:
        _bench = BenchDefinition()

    if graph is not None:
        _graph = graph
    else:
        from .engine.capability_graph import build_capability_graph
        _graph = build_capability_graph(_bench)


def get_bench() -> BenchDefinition:
    """Return the loaded BenchDefinition (empty if not yet initialized)."""
    if _bench is None:
        return BenchDefinition()
    return _bench


def get_capability_graph() -> CapabilityGraph:
    """Return the derived CapabilityGraph."""
    if _graph is None:
        return CapabilityGraph()
    return _graph


def get_box_ip() -> str:
    return _box_ip


def reload_bench() -> None:
    """Re-fetch bench data from the live box."""
    if _box_ip:
        init_state(box_ip=_box_ip)
