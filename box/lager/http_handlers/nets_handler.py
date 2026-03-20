# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Nets HTTP handler for the Lager Box HTTP server.

Provides endpoints to list, update, and delete saved nets.
"""

import json
import logging

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)


def register_nets_routes(app: Flask) -> None:
    """Register nets REST routes with the Flask app."""

    @app.route('/nets/list', methods=['GET'])
    def nets_list():
        """Return full saved nets details."""
        try:
            with open('/etc/lager/saved_nets.json', 'r') as f:
                nets = json.load(f)
            if not isinstance(nets, list):
                nets = []
            return jsonify(nets)
        except FileNotFoundError:
            return jsonify([])
        except (json.JSONDecodeError, TypeError):
            return jsonify([])

    @app.route('/nets/<name>', methods=['PUT'])
    def nets_update(name):
        """Create or replace a net by name."""
        from ..nets.net import Net
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({'error': 'Invalid JSON body'}), 400
        if not data.get('name') or not data.get('role') or not data.get('instrument'):
            return jsonify({'error': 'name, role, and instrument are required'}), 400

        # If the name is changing, delete the old entry first
        if data['name'] != name:
            Net.delete_local_net(name)

        Net.save_local_net(data)
        return jsonify({'ok': True})

    @app.route('/nets/<name>', methods=['DELETE'])
    def nets_delete(name):
        """Delete a net by name."""
        from ..nets.net import Net
        role = request.args.get('role') or None
        deleted = Net.delete_local_net(name, role)
        if not deleted:
            return jsonify({'error': 'Net not found'}), 404
        return jsonify({'ok': True})
