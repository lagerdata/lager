# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Nets HTTP handler for the Lager Box HTTP server.

Provides a read-only endpoint to return full net details from saved_nets.json.
"""

import json
import logging

from flask import Flask, jsonify

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
