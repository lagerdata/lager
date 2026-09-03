# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Per-net metadata HTTP handler for the Lager Box HTTP server.

Serves the user-authored metadata on a saved net -- ``purpose``, ``notes`` and
``tags`` -- as a focused endpoint, so a caller can update it without modelling
the whole net record.

``PUT /nets/<name>`` cannot serve this purpose: it takes a complete net
definition and rederives ``mappings`` and ``scope_points`` from it, so a caller
that only wants to set a sentence of prose would have to round-trip every field
it does not understand -- and drop the ones it does not know about. That is
exactly how the Net-Manager TUI used to lose ``jlink_script``.

Designed for last-write-wins sync with the control plane: the caller sends the
fields it owns plus an ISO 8601 timestamp per field. Timestamps are merged into
``entry["metadata_timestamps"]`` so the next probe can compare the two sides and
decide which is newer.

The field names are the ones ``lager nets describe`` writes and the MCP server
reads (``box/lager/mcp/schemas/net.py``). They are deliberately minimal: one
``purpose`` sentence, free-form ``notes``, and ``tags`` that the planning tools
match on.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request

from ..nets.net import Net

logger = logging.getLogger(__name__)

# The canonical user-authored metadata keys. Closed on purpose: an unknown key
# would be written into saved_nets.json and then silently ignored by everything
# that reads it, which reads to the caller as a successful save.
ALLOWED_FIELDS = ("purpose", "notes", "tags")

_STRING_FIELDS = ("purpose", "notes")
_LIST_FIELDS = ("tags",)

# Where per-net overrides live. An entry here wins over saved_nets.json in the
# MCP bench loader, so a write that lands under one is invisible to agents.
_BENCH_JSON_PATH = "/etc/lager/bench.json"


def _validate_payload(payload: Any) -> Optional[str]:
    """Return an error string, or None when the payload is well formed."""
    if not isinstance(payload, dict):
        return "Body must be a JSON object"

    fields = payload.get("fields")
    timestamps = payload.get("timestamps")
    if fields is None:
        fields = {}
    if timestamps is None:
        timestamps = {}

    if not isinstance(fields, dict) or not isinstance(timestamps, dict):
        return "fields and timestamps must be objects"

    for key in fields:
        if key not in ALLOWED_FIELDS:
            return "Unknown field: %s (expected one of %s)" % (
                key, ", ".join(ALLOWED_FIELDS),
            )

    for key, value in timestamps.items():
        if key not in ALLOWED_FIELDS:
            return "Unknown timestamp field: %s" % key
        if not isinstance(value, str) or not value.strip():
            return "Timestamp for %s must be a non-empty ISO 8601 string" % key
        # A timestamp without its value cannot be reconciled against anything:
        # it would advance the clock on a field this request did not write, and
        # the next probe would then treat a stale value as the newer one.
        if key not in fields:
            return "Timestamp for %s sent without a matching field" % key

    for key in _STRING_FIELDS:
        if key in fields and not isinstance(fields[key], (str, type(None))):
            return "%s must be a string or null" % key

    for key in _LIST_FIELDS:
        if key in fields:
            value = fields[key]
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                return "%s must be an array of strings or null" % key

    return None


def _read_overrides(name: str) -> List[str]:
    """Metadata keys that ``bench.json`` overrides for this net.

    The MCP bench loader applies ``net_overrides`` *after* building a descriptor
    from ``saved_nets.json``, so an overridden field cannot be changed through
    this endpoint -- the write lands, and agents keep seeing the old value. The
    caller is told which keys those are rather than being left to believe a save
    took effect that did not.

    Best effort: a missing or malformed bench.json means no overrides.
    """
    try:
        with open(_BENCH_JSON_PATH, "r", encoding="utf-8") as f:
            bench = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []

    if not isinstance(bench, dict):
        return []

    for override in bench.get("net_overrides") or []:
        if not isinstance(override, dict) or override.get("name") != name:
            continue
        return [k for k in ALLOWED_FIELDS if k in override]
    return []


def _current_metadata(record: Dict[str, Any]) -> Dict[str, Any]:
    """The metadata view of a saved-net record, with defaults filled in."""
    return {
        "purpose": record.get("purpose") or "",
        "notes": record.get("notes") or "",
        "tags": list(record.get("tags") or []),
    }


def _timestamps(record: Dict[str, Any]) -> Dict[str, str]:
    existing = record.get("metadata_timestamps")
    return dict(existing) if isinstance(existing, dict) else {}


def _apply(record: Dict[str, Any], fields: Dict[str, Any], stamps: Dict[str, str]) -> None:
    """Write metadata onto one record.

    Rebinds nested containers rather than mutating them: ``dict(n)`` in the
    caller is a shallow copy, so mutating ``tags`` or ``metadata_timestamps`` in
    place would reach through into the cache's own object.
    """
    for key, value in fields.items():
        if value is None:
            record.pop(key, None)
        elif key in _LIST_FIELDS:
            record[key] = list(value)
        else:
            record[key] = value

    if stamps:
        merged = _timestamps(record)
        merged.update(stamps)
        record["metadata_timestamps"] = merged


def _find(name: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """All saved nets (copied), and the subset matching ``name``."""
    # Copy before mutating: get_local_nets hands back the cache's own dicts, and
    # a failed write would otherwise leave the new metadata live in memory until
    # something invalidated the cache.
    nets = [dict(n) for n in Net.get_local_nets()]
    return nets, [n for n in nets if n.get("name") == name]


def register_net_metadata_routes(app: Flask) -> None:
    """Register the per-net metadata REST routes with the Flask app."""

    @app.route('/nets/<name>/metadata', methods=['GET'])
    def get_net_metadata(name):
        """Read the metadata on a saved net."""
        _, matched = _find(name)
        if not matched:
            return jsonify({'error': "no saved net named '%s'" % name}), 404

        record = matched[0]
        return jsonify({
            'name': name,
            'fields': _current_metadata(record),
            'metadata_timestamps': _timestamps(record),
            'shadowed_by_override': _read_overrides(name),
        })

    @app.route('/nets/<name>/metadata', methods=['PUT'])
    def put_net_metadata(name):
        """Merge metadata into a saved net, leaving every other field alone."""
        payload = request.get_json(force=True, silent=True)
        error = _validate_payload(payload)
        if error or not isinstance(payload, dict):
            return jsonify({'error': error or 'Body must be a JSON object'}), 400

        fields = payload.get("fields") or {}
        stamps = payload.get("timestamps") or {}

        nets, matched = _find(name)
        if not matched:
            return jsonify({'error': "no saved net named '%s'" % name}), 404

        # Every record sharing this name is updated, not just the first. The
        # MCP bench loader builds one descriptor per record, so leaving a
        # same-named sibling behind would make which metadata an agent sees
        # depend on file order.
        for record in matched:
            _apply(record, fields, stamps)

        Net.save_local_nets(nets)

        shadowed = [k for k in _read_overrides(name) if k in fields]
        if shadowed:
            logger.warning(
                "net '%s': %s overridden in bench.json; the saved value will not "
                "reach the MCP server", name, ", ".join(shadowed),
            )
        logger.info(
            "metadata for net '%s' updated (%s) across %d record(s)",
            name, ", ".join(sorted(fields)) or "no fields", len(matched),
        )

        return jsonify({
            'ok': True,
            'name': name,
            'fields': _current_metadata(matched[0]),
            'metadata_timestamps': _timestamps(matched[0]),
            'records_updated': len(matched),
            'shadowed_by_override': shadowed,
        })
