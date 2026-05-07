# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Box-level metadata HTTP handler.

Persists a single human-readable description for the box itself in
``/etc/lager/box_metadata.json``. Mirrors the per-net metadata pattern so the
Stout control plane can sync ``boxes.description`` bidirectionally.
"""

import json
import logging
import os
from typing import Any, Dict

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

BOX_METADATA_PATH = '/etc/lager/box_metadata.json'


def _read_box_metadata() -> Dict[str, Any]:
    try:
        with open(BOX_METADATA_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {'description': None, 'updated_at': None}
    except (json.JSONDecodeError, OSError):
        return {'description': None, 'updated_at': None}

    if not isinstance(data, dict):
        return {'description': None, 'updated_at': None}

    return {
        'description': data.get('description'),
        'updated_at': data.get('updated_at'),
    }


def _atomic_write(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def register_box_metadata_routes(app: Flask) -> None:
    """Register box-metadata REST routes with the Flask app."""

    @app.route('/box-metadata', methods=['GET'])
    def get_box_metadata():
        return jsonify(_read_box_metadata())

    @app.route('/box-metadata', methods=['PUT'])
    def put_box_metadata():
        payload = request.get_json(force=True, silent=True)
        if not isinstance(payload, dict):
            return jsonify({'error': 'Body must be a JSON object'}), 400

        description = payload.get('description')
        updated_at = payload.get('updated_at')

        if description is not None and not isinstance(description, str):
            return jsonify({'error': 'description must be string or null'}), 400
        if updated_at is not None and not isinstance(updated_at, str):
            return jsonify({'error': 'updated_at must be an ISO 8601 string or null'}), 400

        _atomic_write(BOX_METADATA_PATH, {
            'description': description,
            'updated_at': updated_at,
        })
        return jsonify({'ok': True})
