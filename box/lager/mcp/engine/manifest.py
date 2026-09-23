# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Build a ``BenchManifest`` from the loaded bench and capability graph."""

from __future__ import annotations

import copy
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
    it), ``reference_keys`` names the API reference for each net, and
    ``reference_entries`` carries those entries so a consumer that plans
    tests needs nothing else from the box.
    """
    # Imported here: the reference introspects the driver classes on first
    # import, which the :9000 server should pay on the first /bench request
    # rather than at startup.
    from ..data.api_reference import API_REFERENCE

    snapshot = bench.model_copy(deep=True)
    snapshot.capability_bindings = [node.model_dump(mode="json") for node in graph.nodes]

    reference_keys: dict[str, str] = {}
    for net in snapshot.nets:
        key = reference_key(net.net_type)
        if key is not None:
            reference_keys[net.name] = key

    # Deep copies: the entries are the module's live dicts, and a consumer
    # must never be able to reach them through the manifest.
    reference_entries = {
        key: copy.deepcopy(API_REFERENCE[key])
        for key in sorted(set(reference_keys.values()))
        if key in API_REFERENCE
    }

    manifest = BenchManifest(
        generated_at=generated_at or _now_iso(),
        box_id=snapshot.box_id,
        bench=snapshot,
        reference_keys=reference_keys,
        reference_entries=reference_entries,
    )
    manifest.content_hash = manifest.compute_hash()
    return manifest


__all__ = ["build_manifest"]
