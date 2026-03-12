# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Instruments HTTP handler for the Lager Box HTTP server.

Provides a read-only endpoint to detect and return USB instruments
connected to the box, reusing the scan logic from query_instruments.
"""

import logging
import sys

from flask import Flask, jsonify

logger = logging.getLogger(__name__)


def register_instruments_routes(app: Flask) -> None:
    """Register instruments REST routes with the Flask app."""

    @app.route('/instruments/list', methods=['GET'])
    def instruments_list():
        """Scan USB devices and return detected instruments."""
        try:
            # Import scan functions from CLI implementation
            sys.path.insert(0, '/app/box_python')
            from cli.impl.query_instruments import (
                _scan_usb,
                _by_handshake,
                _by_camera,
                _merge_or_append,
            )

            instruments = _scan_usb()
            # Build exclusion list from already-identified UART devices
            uart_ports = {dev.get("tty_path") for dev in instruments if dev.get("tty_path")}
            for dex in _by_handshake(exclude=uart_ports):
                _merge_or_append(dex, instruments)
            for cam in _by_camera():
                _merge_or_append(cam, instruments)

            instruments.sort(key=lambda d: (d["name"], d.get("address", "")))
            return jsonify(instruments)
        except Exception as e:
            logger.error("Failed to scan instruments: %s", e)
            return jsonify({'error': str(e)}), 500
