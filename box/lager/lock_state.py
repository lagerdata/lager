# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Shared lock-state machine used by every HTTP entry point on the box.

The box runs two parallel HTTP servers that both expose `/lock`,
`/unlock`, and `/lock/heartbeat`:

  - Port 5000: the Python execution service (raw stdlib http.server
    via `BaseHTTPRequestHandler` in `lager/python/service.py`). This
    is the endpoint the lager CLI talks to.
  - Port 9000: a Flask server (`lager/http_handlers/lock_handler.py`)
    that wraps the same operations for non-CLI clients (diagnostics,
    integration tests, future tooling).

Both services delegate to this module so the wire shape is identical
no matter which port the caller chose. The previous arrangement had
each service implementing its own lock logic; the port-5000 version
silently lacked `holder_type` / `ttl_seconds` / `/lock/heartbeat` and
caused a full round of confused smoke failures before we noticed.

Lock state file: ``/etc/lager/lock.json``

Schema (all fields optional except ``locked``):

    {
        "locked":         true,
        "user":           "<holder>",
        "holder_type":    "user" | "ci" | "ephemeral" | <reservation origin>,
        "locked_at":      "<ISO 8601 UTC>",
        "last_heartbeat": "<ISO 8601 UTC>",
        "ttl_seconds":    1800 | null
    }

``ttl_seconds: null`` means the lock never auto-expires (used by
``lager boxes lock`` and by ``lager python --detach`` runs). Locks
with a positive ``ttl_seconds`` are reaped when
``last_heartbeat + ttl_seconds`` falls in the past, which gracefully
handles crashed CI runners, killed processes, etc.

Concurrency:
- All reads/read-modify-writes are wrapped in ``fcntl.flock`` on a
  sidecar lockfile so two simultaneous POSTs can't both think they won.
- Writes go through ``tempfile`` + ``os.replace`` so a crashed writer
  never leaves a partial JSON file on disk.

The public API is the four ``acquire`` / ``heartbeat`` / ``release``
/ ``status`` functions. Each returns ``(http_status_code, body_dict)``
and never raises for ordinary contention; callers just forward the
tuple to their respective framework.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

LOCK_FILE = '/etc/lager/lock.json'
LOCK_FILE_GUARD = LOCK_FILE + '.flock'

# Well-known holder types. ``ephemeral`` and ``ci`` are auto-locks (a
# re-acquire may rewrite their classification and TTL); anything else is a
# reservation. Other services (e.g. the web dashboard) write their own origin
# token as ``holder_type``, so normalization preserves any non-empty string
# verbatim — coercing an unrecognized reservation type to ``ephemeral`` would
# give it a TTL and let the reaper silently drop it.
KNOWN_HOLDER_TYPES = ('user', 'ci', 'ephemeral')

# Sentinel meaning "the caller did not supply this field". We can't use
# ``None`` for ``ttl_seconds`` because ``None`` IS a valid value (eternal
# lock). Important for `acquire`: when BOTH ``holder_type`` and
# ``ttl_seconds`` are unset, we treat the request as a legacy payload
# and pick `user` / `None` defaults (preserves backward compat with the
# 0.13.x `lager boxes lock` clients still in the field).
_UNSET = object()


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith('Z'):
            value = value[:-1] + '+00:00'
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# File-level concurrency primitives
# ---------------------------------------------------------------------------


class FileGuard:
    """Context manager holding an exclusive ``fcntl.flock`` on a sidecar.

    We can't flock the lock JSON itself because we replace it via
    tempfile rename. The sidecar exists solely to serialize critical
    sections.

    Resolves the guard path from the module-level ``LOCK_FILE_GUARD`` at
    enter time (not class-definition time) so tests can monkeypatch the
    module global and have it take effect.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path
        self._fd: Optional[int] = None

    def __enter__(self) -> "FileGuard":
        path = self._path or LOCK_FILE_GUARD
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def _read_lock_raw() -> Optional[Dict[str, Any]]:
    """Read raw lock JSON from disk, or ``None`` if missing/invalid."""
    try:
        with open(LOCK_FILE, 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get('locked'):
        return None
    return data


def _is_expired(lock: Dict[str, Any]) -> bool:
    ttl = lock.get('ttl_seconds')
    if ttl is None:
        return False
    last = _parse_iso(lock.get('last_heartbeat') or lock.get('locked_at'))
    if last is None:
        return False
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return elapsed > ttl


def _read_lock() -> Optional[Dict[str, Any]]:
    """Read lock state, transparently reaping expired locks.

    Returns ``None`` if no live lock is held. Callers should hold a
    ``FileGuard`` if they intend to write back, so the read-and-write
    pair is atomic.
    """
    lock = _read_lock_raw()
    if lock is None:
        return None
    if _is_expired(lock):
        logger.info(
            'lock for %s expired (last_heartbeat=%s ttl=%s) - auto-clearing',
            lock.get('user'), lock.get('last_heartbeat'), lock.get('ttl_seconds'),
        )
        _clear_lock()
        return None
    return lock


def _write_lock(data: Dict[str, Any]) -> None:
    """Atomically replace the lock file with ``data``."""
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix='.lock-', suffix='.json',
        dir=os.path.dirname(LOCK_FILE),
    )
    try:
        # mkstemp creates 0o600; the plain open() this replaced produced
        # 0o644. Keep the lock file world-readable so a future deployment
        # where the two HTTP servers run as different users (or any
        # diagnostic reading /etc/lager directly) doesn't break.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, LOCK_FILE)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _clear_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise


# ---------------------------------------------------------------------------
# Payload normalization
# ---------------------------------------------------------------------------


def _normalize_holder_type(value: Any, default: str = 'ephemeral') -> str:
    # Any non-empty string is kept verbatim (see KNOWN_HOLDER_TYPES): only
    # `ephemeral`/`ci` carry special semantics, and unrecognized tokens are
    # reservation origins from other services that must not be reclassified.
    if isinstance(value, str) and value:
        return value
    return default


def _normalize_ttl(value: Any, default: Optional[int] = 1800) -> Optional[int]:
    """Coerce an *explicitly specified* ttl value.

    The caller (``acquire``) decides whether the field was specified at
    all and supplies the appropriate per-holder-type default itself; by
    the time we land here, ``value`` came from the request payload and
    Python ``None`` must mean "the client sent JSON null" (i.e. wants
    an eternal lock), NOT "use the default". Treating None as default
    is the bug that broke ``lager python --detach`` (the CLI sent
    ``ttl_seconds: null`` and the server kept clobbering it to 1800).

    - ``None``                              -> ``None`` (explicit null,
                                                eternal lock).
    - ``"null"`` / ``"none"`` / ``""`` str  -> ``None``.
    - Integer-like                          -> ``max(1, int(value))``.
    - Garbage                               -> ``default``.
    """
    if value is None:
        return None
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


# ---------------------------------------------------------------------------
# Public API used by both HTTP services
# ---------------------------------------------------------------------------


def status() -> Tuple[int, Dict[str, Any]]:
    """GET /lock - return the current lock state.

    Always returns 200 with at least ``{"locked": false}``. When a
    lock is held, returns the full lock dict (including ``locked``,
    ``user``, ``holder_type``, ``locked_at``, ``last_heartbeat``,
    ``ttl_seconds``).
    """
    with FileGuard():
        lock = _read_lock()
    if lock:
        return 200, lock
    return 200, {'locked': False}


def acquire(
    user: str,
    holder_type: Any = _UNSET,
    ttl_seconds: Any = _UNSET,
) -> Tuple[int, Dict[str, Any]]:
    """POST /lock - acquire (or refresh) a lock for ``user``.

    ``holder_type`` and ``ttl_seconds`` are accepted as raw payload
    values; pass ``_UNSET`` (or simply omit) to indicate "not in the
    request payload at all". When both are unset, we treat the payload
    as legacy and apply ``holder_type=user`` + ``ttl_seconds=None`` so
    older `lager boxes lock` clients keep their eternal-lock semantics.

    Returns:
        (200, body) on acquire-or-refresh. Body is the new lock dict
        plus ``previous_user`` (the holder before this call, or
        ``None``). Clients use ``previous_user`` to distinguish
        "we just acquired" from "we already held it".

        (409, {"error", "lock"}) when the box is locked by someone
        else.

        (400, {"error"}) when ``user`` is empty.
    """
    if not user:
        return 400, {'error': 'user is required'}

    legacy = holder_type is _UNSET and ttl_seconds is _UNSET
    if legacy:
        norm_holder_type = 'user'
        norm_ttl: Optional[int] = None
    else:
        # If only one of the two fields was provided, fill in sensible
        # defaults for the other rather than rejecting the payload.
        # Ephemeral is the right default for partial CI-style payloads.
        provided_ht = None if holder_type is _UNSET else holder_type
        norm_holder_type = _normalize_holder_type(
            provided_ht, default='ephemeral',
        )
        # CRITICAL: we must distinguish "field absent" from "field
        # present and null". Both arrive as ``None`` in vanilla JSON
        # decoding; we use the ``_UNSET`` sentinel to tell them apart.
        #   - absent  -> use the per-holder-type default (None for
        #               user, 1800 otherwise). This preserves the
        #               documented contract that `lager boxes lock`
        #               (legacy or with explicit holder_type=user) gives
        #               an eternal lock.
        #   - present -> honor exactly. JSON ``null`` -> Python None ->
        #               eternal. Numbers -> clamp >= 1.
        if ttl_seconds is _UNSET:
            norm_ttl = None if norm_holder_type == 'user' else 1800
        else:
            norm_ttl = _normalize_ttl(ttl_seconds, default=1800)

    with FileGuard():
        existing = _read_lock()
        previous_user = existing.get('user') if existing else None
        if existing and existing.get('user') != user:
            return 409, {
                'error': f'Box is locked by {existing["user"]}',
                'lock': existing,
            }

        now = _now_utc_iso()
        if existing:
            # Same-holder refresh. Only auto-lock types (ephemeral/ci) may
            # have their classification and TTL rewritten by a re-acquire;
            # a reservation (`user`, a dashboard origin, or a legacy record with no
            # holder_type — those were all written by `lager boxes lock`
            # era servers) must survive any number of `lager python` runs
            # untouched, or an eternal lock silently gains a TTL and gets
            # reaped behind the holder's back.
            new_lock = dict(existing)
            new_lock.update({
                'locked': True,
                'user': user,
                'last_heartbeat': now,
            })
            if existing.get('holder_type') in ('ephemeral', 'ci'):
                new_lock['holder_type'] = norm_holder_type
                new_lock['ttl_seconds'] = norm_ttl
        else:
            new_lock = {
                'locked': True,
                'user': user,
                'holder_type': norm_holder_type,
                'locked_at': now,
                'last_heartbeat': now,
                'ttl_seconds': norm_ttl,
            }
        _write_lock(new_lock)

    response = dict(new_lock)
    response['previous_user'] = previous_user
    return 200, response


def heartbeat(user: str) -> Tuple[int, Dict[str, Any]]:
    """POST /lock/heartbeat - refresh ``last_heartbeat``.

    Returns:
        (200, lock dict) on success.
        (404, {"error"}) when the box isn't locked (caller should
            re-acquire).
        (403, {"error", "lock"}) when locked by a different user.
        (400, {"error"}) when ``user`` is empty.
    """
    if not user:
        return 400, {'error': 'user is required'}

    with FileGuard():
        existing = _read_lock()
        if not existing:
            return 404, {'error': 'Box is not locked'}
        if existing.get('user') != user:
            return 403, {
                'error': f'Box is locked by {existing["user"]}',
                'lock': existing,
            }
        existing['last_heartbeat'] = _now_utc_iso()
        _write_lock(existing)

    return 200, existing


def release(user: str, force: bool = False) -> Tuple[int, Dict[str, Any]]:
    """POST /unlock - release a lock.

    ``force=true`` releases even when the holder doesn't match
    (used by `lager boxes unlock --force`).

    Returns:
        (200, {"locked": False, "message": ...}) when the lock was
        cleared (or was already absent).
        (403, {"error", "lock"}) when locked by someone else and
        ``force`` is false.
        (400, {"error"}) when ``user`` is empty.
    """
    if not user:
        return 400, {'error': 'user is required'}

    with FileGuard():
        lock = _read_lock()
        if not lock:
            return 200, {'locked': False, 'message': 'Box is already unlocked'}

        if lock.get('user') != user and not force:
            return 403, {
                'error': f'Box is locked by {lock["user"]}',
                'lock': lock,
            }

        _clear_lock()

    return 200, {'locked': False, 'message': 'Box unlocked'}


# Convenience re-exports so callers can `from lager.lock_state import
# acquire, heartbeat, release, status` and not have to think about
# internals.
__all__ = [
    'acquire',
    'heartbeat',
    'release',
    'status',
    'LOCK_FILE',
    'LOCK_FILE_GUARD',
    'KNOWN_HOLDER_TYPES',
]
