# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Custom-device assignment HTTP handlers for the Lager Box HTTP server.

Exposes the `lager nets assign` backend (lager.devices.assign) on :9000 —
previously the cli/impl/custom_devices.py script executed over :5000.

Routes (JSON contracts identical to the old impl script's stdout):
    GET  /custom-devices/list    -> {"catalog": [...], "assignments": [...],
                                     "cables": [...]}
    POST /custom-devices/assign  -> stored assignment record (+ address, tty,
                                    roles, channels, deleted_nets)
    POST /custom-devices/remove  -> {"removed": bool, "deleted_nets": [...], ...}

User errors (unknown instrument, unplugged/ambiguous cable, bad identity)
return 400 with {"error": ...}; unexpected failures return 500.
"""

import logging

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)


def register_custom_devices_routes(app: Flask) -> None:
    """Register custom-device assignment REST routes with the Flask app."""

    @app.route('/custom-devices/list', methods=['GET'])
    def custom_devices_list():
        try:
            from lager.devices import assign as assign_ops
            return jsonify(assign_ops.list_state())
        except Exception as e:
            logger.exception("Failed to list custom devices")
            return jsonify({'error': str(e)}), 500

    @app.route('/custom-devices/assign', methods=['POST'])
    def custom_devices_assign():
        try:
            from lager.devices import assign as assign_ops
            payload = request.get_json() or {}
            try:
                return jsonify(assign_ops.assign(payload))
            except assign_ops.AssignmentError as e:
                return jsonify({'error': str(e)}), 400
        except Exception as e:
            logger.exception("Failed to assign custom device")
            return jsonify({'error': str(e)}), 500

    @app.route('/custom-devices/remove', methods=['POST'])
    def custom_devices_remove():
        try:
            from lager.devices import assign as assign_ops
            payload = request.get_json() or {}
            try:
                return jsonify(assign_ops.remove(payload))
            except assign_ops.AssignmentError as e:
                return jsonify({'error': str(e)}), 400
        except Exception as e:
            logger.exception("Failed to remove custom device")
            return jsonify({'error': str(e)}), 500
