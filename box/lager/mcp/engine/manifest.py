# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Build a ``BenchManifest`` from the loaded bench and capability graph."""

from __future__ import annotations

from datetime import datetime, timezone

from ..schemas.bench import BenchDefinition
from ..schemas.capability import CapabilityGraph
from ..schemas.manifest import BenchManifest
from .net_types import reference_key


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_manifest(
    bench: BenchDefinition,
    graph: CapabilityGraph,
    *,
    generated_at: str | None = None,
) -> BenchManifest:
    """Assemble the manifest for one bench.

    The bench is copied, never mutated: the caller's object is shared
    server state. ``capability_bindings`` on the copy is filled from the
    graph (the field existed on ``BenchDefinition`` but nothing populated
    it), and ``reference_keys`` names the API reference for each net.
    """
    snapshot = bench.model_copy(deep=True)
    snapshot.capability_bindings = [node.model_dump(mode="json") for node in graph.nodes]

    reference_keys: dict[str, str] = {}
    for net in snapshot.nets:
        key = reference_key(net.net_type)
        if key is not None:
            reference_keys[net.name] = key

    manifest = BenchManifest(
        generated_at=generated_at or _now_iso(),
        box_id=snapshot.box_id,
        bench=snapshot,
        reference_keys=reference_keys,
    )
    manifest.content_hash = manifest.compute_hash()
    return manifest


__all__ = ["build_manifest"]
