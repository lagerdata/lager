# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Webcam HTTP handlers for the Lager Box HTTP server.

NOTE: These endpoints are used by the Stout enterprise platform and are not
part of the core open-source lager CLI feature set. They are kept here because
they have a direct dependency on lager internals (lager.nets and
lager.automation.webcam). They expose webcam stream management to Stout via
the same Flask server that hosts all other box hardware endpoints.

Follows the register_*_routes(app) pattern from supply.py.
"""

import json
import logging
import os

from flask import Flask, request, jsonify

logger = logging.getLogger(__name__)


def register_webcam_routes(app: Flask) -> None:
    """Register webcam REST routes with the Flask app."""

    @app.route('/dashboard/webcam/streams', methods=['GET'])
    def dashboard_webcam_streams():
        streams_path = '/etc/lager/webcam_streams.json'
        if os.path.exists(streams_path):
            with open(streams_path) as f:
                return jsonify(json.load(f))
        return jsonify({})

    @app.route('/dashboard/webcam/start', methods=['POST'])
    def dashboard_webcam_start():
        from lager.nets.net import Net
        from lager.automation.webcam import start_stream

        data = request.get_json() or {}
        net_name = data.get('net')
        if not net_name:
            return jsonify({'error': 'net is required', 'status': 'error'}), 400

        try:
            nets = Net.list_saved()
            net = None
            for n in nets:
                if n.get('name') == net_name:
                    net = n
                    break
            if not net:
                return jsonify({'error': f"Net '{net_name}' not found", 'status': 'error'}), 404
            if net.get('role') != 'webcam':
                return jsonify({'error': f"Net '{net_name}' is not a webcam net", 'status': 'error'}), 400
            video_device = net.get('pin')
            if not video_device:
                return jsonify({'error': f"Net '{net_name}' has no video device configured", 'status': 'error'}), 400
            if not video_device.startswith('/dev/'):
                video_device = f'/dev/{video_device}'
        except Exception as e:
            logger.exception('Failed to resolve video device for net %s', net_name)
            return jsonify({'error': str(e), 'status': 'error'}), 500

        box_ip = request.host.split(':')[0]

        try:
            result = start_stream(net_name, video_device, box_ip)
            return jsonify({'status': 'ok', **result})
        except Exception as e:
            logger.exception('Failed to start webcam stream for %s', net_name)
            return jsonify({'error': str(e), 'status': 'error'}), 500

    @app.route('/dashboard/webcam/stop', methods=['POST'])
    def dashboard_webcam_stop():
        from lager.automation.webcam import stop_stream

        data = request.get_json() or {}
        net_name = data.get('net')
        if not net_name:
            return jsonify({'error': 'net is required', 'status': 'error'}), 400

        try:
            stopped = stop_stream(net_name)
            if stopped:
                return jsonify({'status': 'ok', 'message': f"Stream '{net_name}' stopped"})
            else:
                return jsonify({'error': f"No active stream for '{net_name}'", 'status': 'error'}), 404
        except Exception as e:
            logger.exception('Failed to stop webcam stream for %s', net_name)
            return jsonify({'error': str(e), 'status': 'error'}), 500
