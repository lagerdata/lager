# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Custom-binaries and download-file HTTP handlers for the Lager Box HTTP server.

Exposes on :9000 the same wire contracts the :5000 python-exec service serves
(box/lager/python/service.py); both shim lager.binaries.store so the disk
logic and the download allowlist stay identical.

Routes:
    GET  /binaries/list     -> {"binaries": [...], "host_path": ...,
                                "container_path": ..., "mounted": bool}
    POST /binaries/add      -> multipart form: `binary` (file) + `name`;
                               {"success": true, "name": ..., "path": ...,
                                "size": ..., "restart_required": bool}
    POST /binaries/remove   -> JSON {"name": ...}; {"success": true, "name": ...}
    GET  /download-file?filename=... -> streamed application/octet-stream
                               (allowlisted /tmp/lager-output* roots only)

User errors return the store's status ({"error": ...}); unexpected failures 500.
"""

import logging
import os

from flask import Flask, jsonify, request, send_file

logger = logging.getLogger(__name__)


def register_binaries_routes(app: Flask) -> None:
    """Register custom-binaries and download-file REST routes with the Flask app."""

    @app.route('/binaries/list', methods=['GET'])
    def binaries_list():
        try:
            from lager.binaries import store
            return jsonify(store.list_state())
        except Exception as e:
            logger.exception("Failed to list binaries")
            return jsonify({'error': str(e)}), 500

    @app.route('/binaries/add', methods=['POST'])
    def binaries_add():
        try:
            from lager.binaries import store

            binary_file = request.files.get('binary')
            if binary_file is None:
                return jsonify({'error': 'binary file is required'}), 400

            name = request.form.get('name')
            if not name:
                return jsonify({'error': 'name is required'}), 400

            content = binary_file.read()
            try:
                result = store.add_binary(name, content)
            except store.StoreError as e:
                return jsonify({'error': e.message}), e.status

            logger.info("Binary '%s' uploaded (%d bytes)", name, len(content))
            return jsonify(result)
        except Exception as e:
            logger.exception("Failed to add binary")
            return jsonify({'error': f'Failed to write binary: {e}'}), 500

    @app.route('/binaries/remove', methods=['POST'])
    def binaries_remove():
        try:
            from lager.binaries import store

            payload = request.get_json(silent=True)
            if payload is None:
                return jsonify({'error': 'Invalid JSON'}), 400

            name = payload.get('name')
            try:
                result = store.remove_binary(name)
            except store.StoreError as e:
                return jsonify({'error': e.message}), e.status

            logger.info("Binary '%s' removed", name)
            return jsonify(result)
        except Exception as e:
            logger.exception("Failed to remove binary")
            return jsonify({'error': f'Failed to remove binary: {e}'}), 500

    @app.route('/download-file', methods=['GET'])
    def download_file():
        try:
            from lager.binaries import store

            filename = request.args.get('filename')
            if not filename:
                return jsonify({'error': 'Missing filename parameter'}), 400

            try:
                abs_filename, _size = store.resolve_download_path(filename)
            except store.StoreError as e:
                if e.status == 403:
                    logger.warning(
                        "download-file: rejected path outside allowlist: %r",
                        filename)
                return jsonify({'error': e.message}), e.status

            # Stream the file as-is (NOT gzipped — the CLI handles that).
            return send_file(
                abs_filename,
                mimetype='application/octet-stream',
                as_attachment=True,
                download_name=os.path.basename(filename),
            )
        except Exception as e:
            logger.exception("Failed to download file")
            return jsonify({'error': str(e)}), 500
