# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Shared singleton state for the on-box Lager MCP server.

Initialized at startup from local files (/etc/lager/).
"""

from __future__ import annotations

import logging

from .schemas.bench import BenchDefinition
from .schemas.capability import CapabilityGraph

logger = logging.getLogger(__name__)

_bench: BenchDefinition | None = None
_graph: CapabilityGraph | None = None


def init_state(
    *,
    bench: BenchDefinition | None = None,
    graph: CapabilityGraph | None = None,
) -> None:
    """Bootstrap server state from on-box config files."""
    global _bench, _graph

    if bench is not None:
        _bench = bench
    else:
        from .engine.bench_loader import load_from_files
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


def get_bench() -> BenchDefinition:
    if _bench is None:
        return BenchDefinition()
    return _bench


def get_capability_graph() -> CapabilityGraph:
    if _graph is None:
        return CapabilityGraph()
    return _graph


def reload_bench() -> None:
    """Re-read bench data from local files."""
    init_state()
