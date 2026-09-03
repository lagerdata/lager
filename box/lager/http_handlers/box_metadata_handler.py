# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Box-level metadata HTTP handler for the Lager Box HTTP server.

Persists a single human-readable description of the box itself in
``/etc/lager/box_metadata.json``, alongside the ISO 8601 timestamp of the write
so the control plane can reconcile it last-write-wins against its own copy.

This is deliberately *not* part of ``bench.json``. That file is authored by a
human over SSH (``lager dut edit``) and describes the device under test; this
one is machine-written on every control-plane edit and describes the box -- what
it is, where it lives, who uses it. Folding the two together would mean a
dashboard edit racing an interactive ``$EDITOR`` session over the same file.
"""

import json
import logging
from typing import Any, Dict

from flask import Flask, jsonify, request

from ..constants import BOX_METADATA_PATH
# Reused rather than reimplemented: it stages through `<path>.tmp` and
# os.replace, and cleans the temp file up on failure, so a crashed write can
# never leave a half-written file where the next read expects JSON.
from ..nets.net import _atomic_write_json

logger = logging.getLogger(__name__)

_EMPTY: Dict[str, Any] = {'description': None, 'updated_at': None}


def _read_box_metadata() -> Dict[str, Any]:
    """Current box metadata, or empty values when unset or unreadable.

    Never raises: a box that has never been described, and a box whose file was
    truncated by a bad shutdown, must both read as "no description" rather than
    failing the ``/status`` probe that embeds this.
    """
    try:
        with open(BOX_METADATA_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return dict(_EMPTY)

    if not isinstance(data, dict):
        return dict(_EMPTY)

    description = data.get('description')
    updated_at = data.get('updated_at')
    return {
        'description': description if isinstance(description, str) else None,
        'updated_at': updated_at if isinstance(updated_at, str) else None,
    }


def register_box_metadata_routes(app: Flask) -> None:
    """Register the box-metadata REST routes with the Flask app."""

    @app.route('/box-metadata', methods=['GET'])
    def get_box_metadata():
        """Read the box description and the timestamp of its last write."""
        return jsonify(_read_box_metadata())

    @app.route('/box-metadata', methods=['PUT'])
    def put_box_metadata():
        """Replace the box description.

        Both keys are required to be present in spirit but optional in fact:
        omitting ``description`` clears it. There is no merge to do -- the file
        holds exactly one value.
        """
        payload = request.get_json(force=True, silent=True)
        if not isinstance(payload, dict):
            return jsonify({'error': 'Body must be a JSON object'}), 400

        description = payload.get('description')
        updated_at = payload.get('updated_at')

        if description is not None and not isinstance(description, str):
            return jsonify({'error': 'description must be a string or null'}), 400
        if updated_at is not None and not isinstance(updated_at, str):
            return jsonify({'error': 'updated_at must be an ISO 8601 string or null'}), 400

        record = {'description': description, 'updated_at': updated_at}
        _atomic_write_json(BOX_METADATA_PATH, record)
        logger.info("box description updated (%d chars)", len(description or ''))
        return jsonify({'ok': True, **record})
