# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Build a CapabilityGraph from a BenchDefinition.

The graph is *derived* -- each net's type is mapped, through the one net-type
table in ``engine.net_types``, to a set of CapabilityNodes describing what
the bench can actually *do*, not just what is physically connected.

Confidences are intentionally conservative: below 1.0 when the role depends
on instrument firmware or configuration that cannot be verified statically.
"""

from __future__ import annotations

from ..schemas.bench import BenchDefinition
from ..schemas.capability import CapabilityGraph, CapabilityNode, CapabilityRole
from ..schemas.net import NetDescriptor
from .net_types import info_for


def _nodes_for_net(net: NetDescriptor) -> list[CapabilityNode]:
    """Derive capability nodes from a single net."""
    info = info_for(net.net_type)
    if info is None:
        return []
    return [
        CapabilityNode(role=role, target=net.name, confidence=confidence)
        for role, confidence in info.capabilities
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_capability_graph(bench: BenchDefinition) -> CapabilityGraph:
    """
    Derive the full capability graph for a bench definition.

    Returns a CapabilityGraph whose nodes represent every role the bench
    can play, keyed to the specific nets / interfaces that provide it.
    """
    nodes: list[CapabilityNode] = []

    for net in bench.nets:
        nodes.extend(_nodes_for_net(net))

    # Every box supports local program execution
    nodes.append(
        CapabilityNode(
            role=CapabilityRole.RUN_LOCAL_PROGRAM,
            target="_box",
            confidence=1.0,
            notes="On-box Python execution via lager python service",
        )
    )

    return CapabilityGraph(box_id=bench.box_id, nodes=nodes)
