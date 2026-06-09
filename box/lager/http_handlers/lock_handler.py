# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Lock HTTP handler for the Lager Box HTTP server.

Provides endpoints to lock/unlock a box so that shared users
can prevent others from using a box while they're working with it.

Lock state file: /etc/lager/lock.json

Schema (all fields optional except ``locked`` and ``user``):

    {
        "locked": true,
        "user": "<holder>",
        "holder_type": "user" | "ci" | "ephemeral" | "stout",
        "locked_at":      "<ISO 8601 UTC>",
        "last_heartbeat": "<ISO 8601 UTC>",
        "ttl_seconds":    1800 | null
    }

``ttl_seconds: null`` means the lock never auto-expires (used by
``lager boxes lock`` and by ``lager python --detach`` runs). Locks with a
positive ``ttl_seconds`` are reaped when ``last_heartbeat + ttl_seconds``
falls in the past, which gracefully handles crashed CI runners, killed
processes, etc.

Concurrency:
- All reads / read-modify-writes are wrapped in ``fcntl.flock`` on a
  sidecar lockfile so two simultaneous POSTs can't both think they won.
- Writes go through a temp-file + ``os.replace`` so a crashed writer
  never leaves a partial JSON file on disk.
"""

import errno
import fcntl
import json
import logging
import os
import tempfile
from datetime import datetime, timezone

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

LOCK_FILE = '/etc/lager/lock.json'
LOCK_FILE_GUARD = LOCK_FILE + '.flock'

_VALID_HOLDER_TYPES = ('user', 'ci', 'ephemeral', 'stout')


def _now_utc_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse_iso(value):
    if not value:
        return None
    try:
        # tolerate trailing Z
        if value.endswith('Z'):
            value = value[:-1] + '+00:00'
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class _FileGuard:
    """Context manager that holds an exclusive ``fcntl.flock`` on a sidecar.

    We can't flock the lock JSON itself because we replace it via tempfile
    rename. The sidecar exists solely to serialize critical sections.

    The guard path is resolved from the module-level ``LOCK_FILE_GUARD`` at
    enter time, not at class-definition time, so tests can monkeypatch
    ``LOCK_FILE_GUARD`` to redirect into a tmpdir.
    """

    def __init__(self, path=None):
        self._path = path  # if None, resolved from module global on enter
        self._fd = None

    def __enter__(self):
        path = self._path or LOCK_FILE_GUARD
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # O_CREAT|O_RDWR is enough; we never read/write contents.
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def _read_lock_raw():
    """Read raw lock JSON from disk, or ``None`` if missing/invalid."""
    try:
        with open(LOCK_FILE, 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get('locked'):
        return None
    return data


def _is_expired(lock):
    ttl = lock.get('ttl_seconds')
    if ttl is None:
        return False
    last = _parse_iso(lock.get('last_heartbeat') or lock.get('locked_at'))
    if last is None:
        return False
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return elapsed > ttl


def _read_lock():
    """Read lock state, transparently reaping expired locks.

    Returns ``None`` if no live lock is held. Must be called under
    ``_FileGuard`` for any subsequent write-back to be race-free.
    """
    lock = _read_lock_raw()
    if lock is None:
        return None
    if _is_expired(lock):
        logger.info(
            'lock for %s expired (last_heartbeat=%s ttl=%s) — auto-clearing',
            lock.get('user'), lock.get('last_heartbeat'), lock.get('ttl_seconds'),
        )
        _clear_lock()
        return None
    return lock


def _write_lock(data):
    """Atomically replace the lock file with ``data``.

    Uses ``tempfile + os.replace`` so concurrent readers never observe a
    half-written JSON document.
    """
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix='.lock-', suffix='.json',
        dir=os.path.dirname(LOCK_FILE),
    )
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, LOCK_FILE)
    except Exception:
        # Best-effort cleanup of the temp file on failure.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _clear_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise


def _normalize_holder_type(value, default='ephemeral'):
    if value in _VALID_HOLDER_TYPES:
        return value
    return default


def _normalize_ttl(value, default=1800):
    """Normalize ``ttl_seconds`` from request payload.

    - Absent / ``None`` -> ``default``.
    - ``"null"`` / ``"none"`` (string) -> ``None`` (eternal).
    - Integer-like -> ``max(1, int(value))``.
    - Garbage -> ``default``.
    """
    if value is None:
        return default
    if isinstance(value, str):
        if value.lower() in ('null', 'none', ''):
            return None
        try:
            return max(1, int(value))
        except ValueError:
            return default
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def register_lock_routes(app: Flask) -> None:
    """Register lock REST routes with the Flask app."""

    @app.route('/lock', methods=['GET'])
    def lock_status():
        """Return current lock status."""
        with _FileGuard():
            lock = _read_lock()
        if lock:
            return jsonify(lock)
        return jsonify({'locked': False})

    @app.route('/lock', methods=['POST'])
    def lock_box():
        """Lock the box for a user.

        Request body:
            {
                "user": "<holder>",                  # required
                "holder_type": "user"|"ci"|...,      # optional, default "ephemeral"
                "ttl_seconds": int | null            # optional, default 1800
            }

        Response 200 (acquired or already-ours) includes the lock JSON plus
        ``previous_user``: the holder string before this POST (``None`` if
        the box was unlocked). Clients use this to distinguish "we just
        acquired" from "we already held it".

        Response 409: locked by someone else. Body contains ``lock``: the
        live lock JSON.
        """
        data = request.get_json(silent=True) or {}
        user = data.get('user')
        if not user:
            return jsonify({'error': 'user is required'}), 400

        # Legacy `lager boxes lock` clients send only ``{"user": ...}``. The
        # documented behaviour ("user locks do not expire") means we must
        # default to ``holder_type=user`` + ``ttl_seconds=null`` when neither
        # field is present in the payload. New clients send both fields
        # explicitly and we honour them as given.
        legacy_payload = 'holder_type' not in data and 'ttl_seconds' not in data
        if legacy_payload:
            holder_type = 'user'
            ttl_seconds = None
        else:
            holder_type = _normalize_holder_type(
                data.get('holder_type'),
                default='ephemeral',
            )
            ttl_seconds = _normalize_ttl(
                data.get('ttl_seconds', None),
                default=None if holder_type == 'user' else 1800,
            )

        with _FileGuard():
            existing = _read_lock()
            previous_user = existing.get('user') if existing else None
            if existing and existing.get('user') != user:
                return jsonify({
                    'error': f'Box is locked by {existing["user"]}',
                    'lock': existing,
                }), 409

            now = _now_utc_iso()
            if existing:
                # Same holder re-acquiring: refresh fields but keep locked_at.
                new_lock = dict(existing)
                new_lock.update({
                    'locked': True,
                    'user': user,
                    'holder_type': holder_type,
                    'last_heartbeat': now,
                    'ttl_seconds': ttl_seconds,
                })
            else:
                new_lock = {
                    'locked': True,
                    'user': user,
                    'holder_type': holder_type,
                    'locked_at': now,
                    'last_heartbeat': now,
                    'ttl_seconds': ttl_seconds,
                }
            _write_lock(new_lock)

        response = dict(new_lock)
        response['previous_user'] = previous_user
        return jsonify(response)

    @app.route('/lock/heartbeat', methods=['POST'])
    def lock_heartbeat():
        """Refresh ``last_heartbeat`` so the lock isn't reaped by TTL.

        Request body: ``{"user": "<holder>"}``.

        - 200 if ``user`` matches the current holder; ``last_heartbeat`` is
          updated.
        - 404 if the box isn't locked (caller should re-acquire).
        - 403 if locked by someone else (the heartbeat will not refresh
          another holder's lock).
        """
        data = request.get_json(silent=True) or {}
        user = data.get('user')
        if not user:
            return jsonify({'error': 'user is required'}), 400

        with _FileGuard():
            existing = _read_lock()
            if not existing:
                return jsonify({'error': 'Box is not locked'}), 404
            if existing.get('user') != user:
                return jsonify({
                    'error': f'Box is locked by {existing["user"]}',
                    'lock': existing,
                }), 403
            existing['last_heartbeat'] = _now_utc_iso()
            _write_lock(existing)

        return jsonify(existing)

    @app.route('/unlock', methods=['POST'])
    def unlock_box():
        """Unlock the box."""
        data = request.get_json(silent=True) or {}
        user = data.get('user')
        force = data.get('force', False)

        if not user:
            return jsonify({'error': 'user is required'}), 400

        with _FileGuard():
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
