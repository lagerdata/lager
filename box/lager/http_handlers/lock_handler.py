# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Lock HTTP handler for the Lager Box HTTP server.

Provides endpoints to lock/unlock a box so that shared users
can prevent others from using a box while they're working with it.

Lock state file: /etc/lager/lock.json
"""

import json
import logging
from datetime import datetime, timezone

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

LOCK_FILE = '/etc/lager/lock.json'


def _read_lock():
    """Read lock state from disk. Returns None if unlocked."""
    try:
        with open(LOCK_FILE, 'r') as f:
            data = json.load(f)
        if data.get('locked'):
            return data
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        pass
    return None


def _write_lock(user):
    """Write lock state to disk."""
    data = {
        'locked': True,
        'user': user,
        'locked_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    with open(LOCK_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    return data


def _clear_lock():
    """Remove lock state."""
    import os
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


def register_lock_routes(app: Flask) -> None:
    """Register lock REST routes with the Flask app."""

    @app.route('/lock', methods=['GET'])
    def lock_status():
        """Return current lock status."""
        lock = _read_lock()
        if lock:
            return jsonify(lock)
        return jsonify({'locked': False})

    @app.route('/lock', methods=['POST'])
    def lock_box():
        """Lock the box for a user."""
        data = request.get_json(silent=True) or {}
        user = data.get('user')
        if not user:
            return jsonify({'error': 'user is required'}), 400

        lock = _read_lock()
        if lock:
            if lock.get('user') == user:
                # Already locked by this user
                return jsonify(lock)
            # Locked by someone else
            return jsonify({
                'error': f'Box is locked by {lock["user"]}',
                'lock': lock,
            }), 409

        new_lock = _write_lock(user)
        return jsonify(new_lock)

    @app.route('/unlock', methods=['POST'])
    def unlock_box():
        """Unlock the box."""
        data = request.get_json(silent=True) or {}
        user = data.get('user')
        force = data.get('force', False)

        if not user:
            return jsonify({'error': 'user is required'}), 400

        lock = _read_lock()
        if not lock:
            return jsonify({'locked': False, 'message': 'Box is already unlocked'})

        if lock.get('user') != user and not force:
            return jsonify({
                'error': f'Box is locked by {lock["user"]}',
                'lock': lock,
            }), 403

        _clear_lock()
        return jsonify({'locked': False, 'message': 'Box unlocked'})
