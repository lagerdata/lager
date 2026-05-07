# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Net metadata HTTP handler.

Exposes a focused endpoint for updating per-net metadata
(description, dut_connection, tags, test_hints) without requiring the caller
to know the full net record.

Designed for last-write-wins sync from the Stout control plane: the caller
provides the field values it owns plus an ISO 8601 timestamp per field. The
timestamps are merged into ``entry["metadata_timestamps"]`` so the next probe
from Stout can compare and reconcile.
"""

import logging
from typing import Any, Dict

from flask import Flask, jsonify, request

from ..nets.net import Net

logger = logging.getLogger(__name__)

ALLOWED_FIELDS = ("description", "dut_connection", "tags", "test_hints")


def _validate_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "Body must be a JSON object"

    fields = payload.get("fields") or {}
    timestamps = payload.get("timestamps") or {}

    if not isinstance(fields, dict) or not isinstance(timestamps, dict):
        return "fields and timestamps must be objects"

    for key in fields:
        if key not in ALLOWED_FIELDS:
            return f"Unknown field: {key}"

    for key in timestamps:
        if key not in ALLOWED_FIELDS:
            return f"Unknown timestamp field: {key}"
        if not isinstance(timestamps[key], str):
            return f"Timestamp for {key} must be an ISO 8601 string"

    if isinstance(fields.get("description"), (str, type(None))) is False:
        return "description must be string or null"
    if isinstance(fields.get("dut_connection"), (str, type(None))) is False:
        return "dut_connection must be string or null"

    for arr_field in ("tags", "test_hints"):
        if arr_field in fields:
            value = fields[arr_field]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                return f"{arr_field} must be an array of strings"

    return None


def _apply_metadata(entry: Dict[str, Any], fields: Dict[str, Any], timestamps: Dict[str, str]) -> None:
    for key, value in fields.items():
        entry[key] = value

    if timestamps:
        existing_ts = entry.get("metadata_timestamps")
        if not isinstance(existing_ts, dict):
            existing_ts = {}
        existing_ts.update(timestamps)
        entry["metadata_timestamps"] = existing_ts


def register_net_metadata_routes(app: Flask) -> None:
    """Register net-metadata REST routes with the Flask app."""

    @app.route('/nets/<name>/metadata', methods=['PUT'])
    def put_net_metadata(name: str):
        payload = request.get_json(force=True, silent=True)
        error = _validate_payload(payload)
        if error:
            return jsonify({'error': error}), 400

        local_nets = Net.get_local_nets()
        target = next((n for n in local_nets if n.get("name") == name), None)
        if target is None:
            return jsonify({'error': f"Net '{name}' not found"}), 404

        _apply_metadata(target, payload.get("fields") or {}, payload.get("timestamps") or {})
        Net.save_local_nets(local_nets)
        return jsonify({'ok': True, 'metadata_timestamps': target.get("metadata_timestamps", {})})
