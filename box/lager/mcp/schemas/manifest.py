# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Bench manifest schema -- the versioned, hashable description of one box.

The manifest is what a client that keeps a copy of the bench (a control
plane, a fleet-level MCP host, ``lager bench export``) fetches and stores.
It wraps the ``BenchDefinition`` the MCP tools already read with:

- ``schema_version``, so a consumer can tell which fields to expect;
- ``content_hash``, a SHA-256 over everything except the timestamp and the
  hash itself, so a consumer can skip an unchanged manifest (the box's
  ``GET /bench`` answers ``304`` to a matching ``If-None-Match``);
- ``reference_keys``, which ``lager://reference/{net_type}`` entry documents
  each net, so a consumer can fetch the API reference for exactly the types
  on this bench.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field

from .bench import BenchDefinition

#: Bump when a consumer could misread an older manifest. Adding a field with
#: a default does not need a bump; renaming or re-typing one does.
MANIFEST_SCHEMA_VERSION = 1

#: Fields that describe the manifest rather than the bench, left out of the
#: hash so two manifests of the same bench compare equal.
_UNHASHED_FIELDS = frozenset({"generated_at", "content_hash"})


class BenchManifest(BaseModel):
    """One box's bench, DUT and capabilities, as a versioned document."""

    schema_version: int = MANIFEST_SCHEMA_VERSION
    #: ISO 8601 UTC timestamp of when this manifest was built.
    generated_at: str = ""
    #: SHA-256 hex digest; see ``compute_hash``.
    content_hash: str = ""
    #: The box identifier, repeated from ``bench.box_id`` so a consumer that
    #: holds many manifests can index them without descending into ``bench``.
    box_id: str = ""
    bench: BenchDefinition = Field(default_factory=BenchDefinition)
    #: Net name -> ``API_REFERENCE`` key (``"psu1": "PowerSupply"``). Nets of
    #: a type with no reference are absent.
    reference_keys: dict[str, str] = Field(default_factory=dict)

    def hashed_payload(self) -> dict:
        """The JSON-ready view of this manifest that ``content_hash`` covers."""
        return self.model_dump(mode="json", exclude=set(_UNHASHED_FIELDS))

    def compute_hash(self) -> str:
        """SHA-256 of the canonical JSON of ``hashed_payload``.

        Canonical means sorted keys and no whitespace, so the digest depends
        on the content alone and not on how a serializer happened to lay it
        out.
        """
        canonical = json.dumps(
            self.hashed_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["MANIFEST_SCHEMA_VERSION", "BenchManifest"]
