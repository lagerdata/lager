# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Instruments HTTP handler for the Lager Box HTTP server.

Provides a read-only endpoint to detect and return USB instruments
connected to the box. Runs the scan as a subprocess so that
signal-based timeouts (SIGALRM) work correctly outside the main thread.
"""

import json
import logging
import subprocess
import sys

from flask import Flask, jsonify

logger = logging.getLogger(__name__)

_SCAN_SCRIPT = '/app/box_python/cli/impl/query_instruments.py'
_SCAN_TIMEOUT = 30  # seconds — matches CLI timeout


def register_instruments_routes(app: Flask) -> None:
    """Register instruments REST routes with the Flask app."""

    @app.route('/instruments/list', methods=['GET'])
    def instruments_list():
        """Scan USB devices and return detected instruments."""
        try:
            result = subprocess.run(
                [sys.executable, _SCAN_SCRIPT],
                capture_output=True,
                text=True,
                timeout=_SCAN_TIMEOUT,
            )
            if result.returncode != 0:
                logger.warning(
                    "Instrument scan exited %d: %s",
                    result.returncode,
                    result.stderr[:500],
                )
            instruments = json.loads(result.stdout or '[]')
            return jsonify(instruments)
        except subprocess.TimeoutExpired:
            logger.error("Instrument scan timed out after %ds", _SCAN_TIMEOUT)
            return jsonify({'error': 'Scan timed out'}), 504
        except Exception as e:
            logger.error("Failed to scan instruments: %s", e)
            return jsonify({'error': str(e)}), 500
