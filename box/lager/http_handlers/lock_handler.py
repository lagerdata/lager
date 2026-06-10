# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Flask shim that exposes the shared lock state over the port-9000 server.

All real logic lives in ``lager.lock_state``. This file is intentionally
small: it just translates Flask requests into ``lock_state`` calls so the
port-9000 endpoint behaves identically to the port-5000 endpoint inside
``lager/python/service.py`` (which uses the raw ``http.server`` stack).

If you change behavior, change it in ``lock_state.py`` so both ports
stay in sync.
"""

from __future__ import annotations

from flask import Flask, jsonify, request

from .. import lock_state

# Reuse the sentinel from lock_state so identity comparisons work.
# (Having our own ``object()`` here would silently fall through to the
# ephemeral default, breaking legacy `lager boxes lock` clients.)
_UNSET = lock_state._UNSET  # noqa: SLF001


def register_lock_routes(app: Flask) -> None:
    """Register lock REST routes with the Flask app."""

    @app.route('/lock', methods=['GET'])
    def lock_status():
        code, body = lock_state.status()
        return jsonify(body), code

    @app.route('/lock', methods=['POST'])
    def lock_box():
        data = request.get_json(silent=True) or {}
        user = data.get('user')
        holder_type = data['holder_type'] if 'holder_type' in data else _UNSET
        ttl_seconds = data['ttl_seconds'] if 'ttl_seconds' in data else _UNSET
        code, body = lock_state.acquire(
            user=user, holder_type=holder_type, ttl_seconds=ttl_seconds,
        )
        return jsonify(body), code

    @app.route('/lock/heartbeat', methods=['POST'])
    def lock_heartbeat():
        data = request.get_json(silent=True) or {}
        code, body = lock_state.heartbeat(user=data.get('user'))
        return jsonify(body), code

    @app.route('/unlock', methods=['POST'])
    def unlock_box():
        data = request.get_json(silent=True) or {}
        code, body = lock_state.release(
            user=data.get('user'), force=bool(data.get('force', False)),
        )
        return jsonify(body), code
