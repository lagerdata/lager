# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Instruments HTTP handler for the Lager Box HTTP server.

Provides a read-only endpoint to detect and return USB instruments
connected to the box, reusing the scan logic from usb_scanner.
"""

import logging

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)


def register_instruments_routes(app: Flask) -> None:
    """Register instruments REST routes with the Flask app."""

    @app.route('/instruments/list', methods=['GET'])
    def instruments_list():
        """Scan USB devices and return detected instruments."""
        # The scan is read-only for every instrument except the arm, whose
        # detection writes a G-code handshake at candidate ports. Naming the
        # requester is what makes an unexpected write traceable to the caller
        # that caused it -- there is no cache and no timer, so every one of
        # these lines is one full re-probe.
        logger.info("instruments scan requested by %s", request.remote_addr)
        try:
            from lager.http_handlers.usb_scanner import list_instruments
            return jsonify(list_instruments())
        except Exception as e:
            logger.error("Failed to scan instruments: %s", e)
            return jsonify({'error': str(e)}), 500
