# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for box/lager/lock_state.py.

``lock_state`` is the single source of truth for box-side lock behavior;
both `lager/http_handlers/lock_handler.py` (Flask, port 9000) and
`lager/python/service.py` (raw http.server, port 5000) delegate to it.

The Flask shim is exercised once at the bottom to make sure the wiring
hasn't drifted. The 5000 raw-http shim isn't exercised here because it
ships inside a BaseHTTPRequestHandler subclass that doesn't have a
ready-made test client; the hardware smoke (test/manual/) covers it
end-to-end.

``LOCK_FILE`` / ``LOCK_FILE_GUARD`` are redirected to a tmpdir so we
never touch ``/etc/lager``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
_LOCK_STATE_PY = os.path.join(_REPO_ROOT, "box", "lager", "lock_state.py")


def _load_lock_state():
    """Load ``box/lager/lock_state.py`` without going through ``import
    lager`` (which would pull in box-only deps like pyvisa)."""
    name = "lock_state_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _LOCK_STATE_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def lock_state(tmp_path, monkeypatch):
    """``lock_state`` module with disk paths redirected at tmpdir."""
    ls = _load_lock_state()
    lock_file = tmp_path / "lock.json"
    guard_file = tmp_path / "lock.json.flock"
    monkeypatch.setattr(ls, "LOCK_FILE", str(lock_file))
    monkeypatch.setattr(ls, "LOCK_FILE_GUARD", str(guard_file))
    # Sidecar for tests to read the raw on-disk state.
    ls._test_lock_file = lock_file  # type: ignore[attr-defined]
    return ls


# ---------------------------------------------------------------------------
# acquire()
# ---------------------------------------------------------------------------


class TestAcquire:
    def test_legacy_payload_defaults_to_eternal_user_lock(self, lock_state):
        code, body = lock_state.acquire(user="alice")
        assert code == 200
        assert body["locked"] is True
        assert body["user"] == "alice"
        assert body["holder_type"] == "user"
        assert body["ttl_seconds"] is None
        assert body["previous_user"] is None

        on_disk = json.loads(lock_state._test_lock_file.read_text())
        assert on_disk["holder_type"] == "user"
        assert on_disk["ttl_seconds"] is None

    def test_explicit_user_lock(self, lock_state):
        code, body = lock_state.acquire(
            user="alice", holder_type="user", ttl_seconds=None,
        )
        assert code == 200
        assert body["holder_type"] == "user"
        assert body["ttl_seconds"] is None

    def test_ephemeral_lock_defaults_ttl_to_1800(self, lock_state):
        code, body = lock_state.acquire(
            user="ci:github:repo#1-1/job@runner:42", holder_type="ci",
        )
        assert code == 200
        assert body["holder_type"] == "ci"
        assert body["ttl_seconds"] == 1800

    def test_unknown_holder_type_falls_back_to_ephemeral(self, lock_state):
        code, body = lock_state.acquire(user="alice", holder_type="bogus")
        assert code == 200
        assert body["holder_type"] == "ephemeral"

    def test_reacquire_same_holder_returns_previous_user(self, lock_state):
        lock_state.acquire(user="alice")
        code, body = lock_state.acquire(user="alice")
        assert code == 200
        assert body["previous_user"] == "alice"

    def test_collision_returns_409(self, lock_state):
        lock_state.acquire(user="alice")
        code, body = lock_state.acquire(user="bob")
        assert code == 409
        assert "locked by alice" in body["error"]
        assert body["lock"]["user"] == "alice"

    def test_missing_user_is_400(self, lock_state):
        code, body = lock_state.acquire(user="")
        assert code == 400

    def test_normalize_ttl_accepts_string_null(self, lock_state):
        code, body = lock_state.acquire(
            user="alice", holder_type="user", ttl_seconds="null",
        )
        assert code == 200
        assert body["ttl_seconds"] is None

    def test_normalize_ttl_clamps_negative_to_one(self, lock_state):
        code, body = lock_state.acquire(
            user="alice", holder_type="ci", ttl_seconds=-5,
        )
        assert code == 200
        assert body["ttl_seconds"] == 1

    def test_partial_payload_only_holder_type_uses_ci_default_ttl(self, lock_state):
        # holder_type provided but ttl_seconds absent -> not legacy,
        # ephemeral default doesn't apply because holder_type was given,
        # ttl defaults to 1800.
        code, body = lock_state.acquire(user="alice", holder_type="ci")
        assert code == 200
        assert body["holder_type"] == "ci"
        assert body["ttl_seconds"] == 1800

    def test_explicit_null_ttl_with_ephemeral_holds_eternal(self, lock_state):
        """`lager python --detach` sends ttl_seconds=None with
        holder_type=ephemeral. The server MUST honor that explicit null
        (eternal lock) rather than clobbering it back to the ephemeral
        default of 1800 — the detached run outlives the CLI and there's
        nobody to heartbeat.

        Regression: hardware smoke 2026-06-10 ran with the old code and
        saw ttl_seconds=1800 in the response; the fix in _normalize_ttl
        + acquire's _UNSET branch keeps None -> None.
        """
        code, body = lock_state.acquire(
            user="detached-runner", holder_type="ephemeral", ttl_seconds=None,
        )
        assert code == 200
        assert body["holder_type"] == "ephemeral"
        assert body["ttl_seconds"] is None

    def test_absent_ttl_with_ephemeral_still_defaults_to_1800(self, lock_state):
        # Same holder_type but field truly absent — must still default
        # to 1800. Guards against an over-correction where we'd treat
        # both absent and explicit-null as eternal.
        code, body = lock_state.acquire(user="alice", holder_type="ephemeral")
        assert code == 200
        assert body["ttl_seconds"] == 1800


# ---------------------------------------------------------------------------
# heartbeat()
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_refreshes_last_heartbeat(self, lock_state):
        lock_state.acquire(user="alice", holder_type="ci", ttl_seconds=1800)
        _, first = lock_state.status()
        first_hb = first["last_heartbeat"]

        import time
        time.sleep(1.1)
        code, _ = lock_state.heartbeat(user="alice")
        assert code == 200
        _, second = lock_state.status()
        assert second["last_heartbeat"] != first_hb
        assert second["last_heartbeat"] > first_hb

    def test_heartbeat_404_when_unlocked(self, lock_state):
        code, body = lock_state.heartbeat(user="alice")
        assert code == 404

    def test_heartbeat_403_for_other_holder(self, lock_state):
        lock_state.acquire(user="alice", holder_type="ci", ttl_seconds=1800)
        code, body = lock_state.heartbeat(user="bob")
        assert code == 403

    def test_heartbeat_requires_user(self, lock_state):
        code, body = lock_state.heartbeat(user="")
        assert code == 400


# ---------------------------------------------------------------------------
# TTL auto-reap
# ---------------------------------------------------------------------------


class TestTTL:
    def _stale_heartbeat(self, lock_state, seconds_ago):
        path = lock_state._test_lock_file
        data = json.loads(path.read_text())
        past = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        data["last_heartbeat"] = past
        path.write_text(json.dumps(data))

    def test_expired_lock_is_auto_reaped_on_read(self, lock_state):
        lock_state.acquire(user="alice", holder_type="ci", ttl_seconds=10)
        self._stale_heartbeat(lock_state, seconds_ago=300)
        code, body = lock_state.status()
        assert body == {"locked": False}
        assert not lock_state._test_lock_file.exists()

    def test_eternal_lock_never_expires(self, lock_state):
        lock_state.acquire(user="alice", holder_type="user", ttl_seconds=None)
        self._stale_heartbeat(lock_state, seconds_ago=99999)
        code, body = lock_state.status()
        assert body["locked"] is True
        assert body["user"] == "alice"

    def test_expired_lock_lets_new_holder_acquire(self, lock_state):
        lock_state.acquire(user="alice", holder_type="ci", ttl_seconds=10)
        self._stale_heartbeat(lock_state, seconds_ago=300)
        code, body = lock_state.acquire(user="bob", holder_type="ci")
        assert code == 200
        assert body["user"] == "bob"


# ---------------------------------------------------------------------------
# release()
# ---------------------------------------------------------------------------


class TestRelease:
    def test_release_own(self, lock_state):
        lock_state.acquire(user="alice")
        code, body = lock_state.release(user="alice")
        assert code == 200
        assert body["locked"] is False

    def test_release_other_without_force_403(self, lock_state):
        lock_state.acquire(user="alice")
        code, _ = lock_state.release(user="bob")
        assert code == 403

    def test_release_other_with_force(self, lock_state):
        lock_state.acquire(user="alice")
        code, body = lock_state.release(user="bob", force=True)
        assert code == 200
        assert body["locked"] is False

    def test_release_when_already_unlocked(self, lock_state):
        code, body = lock_state.release(user="alice")
        assert code == 200
        assert body["locked"] is False


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


class TestStatus:
    def test_unlocked(self, lock_state):
        code, body = lock_state.status()
        assert code == 200
        assert body == {"locked": False}

    def test_locked_returns_full_record(self, lock_state):
        lock_state.acquire(user="alice", holder_type="ci", ttl_seconds=300)
        code, body = lock_state.status()
        assert code == 200
        assert body["locked"] is True
        assert body["user"] == "alice"
        assert body["ttl_seconds"] == 300
        assert "locked_at" in body
        assert "last_heartbeat" in body


# ---------------------------------------------------------------------------
# Flask shim wiring smoke test
# ---------------------------------------------------------------------------
#
# We don't reproduce every scenario above through the Flask client (that
# would just be re-testing lock_state). We DO verify that the four
# routes are registered and pass parameters through to lock_state
# without dropping fields — those are the failures that would otherwise
# slip past the unit tests and only show up on hardware.


class TestFlaskShim:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        # The shim does `from .. import lock_state`. We can't load it
        # via bare spec_from_file_location without setting up the
        # parent package, so we shortcut: load lock_state into a fake
        # package and load lock_handler from that package.
        import types

        pkg_name = "lager_under_test_shim"
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [os.path.join(_REPO_ROOT, "box", "lager")]
        sys.modules[pkg_name] = pkg

        ls_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.lock_state", _LOCK_STATE_PY,
        )
        ls = importlib.util.module_from_spec(ls_spec)
        sys.modules[ls_spec.name] = ls
        ls_spec.loader.exec_module(ls)

        # Redirect lock_state's disk paths.
        lock_file = tmp_path / "lock.json"
        guard_file = tmp_path / "lock.json.flock"
        monkeypatch.setattr(ls, "LOCK_FILE", str(lock_file))
        monkeypatch.setattr(ls, "LOCK_FILE_GUARD", str(guard_file))

        # Set up the http_handlers subpackage.
        sub_name = f"{pkg_name}.http_handlers"
        sub = types.ModuleType(sub_name)
        sub.__path__ = [os.path.join(_REPO_ROOT, "box", "lager", "http_handlers")]
        sys.modules[sub_name] = sub

        lh_spec = importlib.util.spec_from_file_location(
            f"{sub_name}.lock_handler",
            os.path.join(_REPO_ROOT, "box", "lager", "http_handlers", "lock_handler.py"),
        )
        lh = importlib.util.module_from_spec(lh_spec)
        sys.modules[lh_spec.name] = lh
        lh_spec.loader.exec_module(lh)

        from flask import Flask
        app = Flask(__name__)
        lh.register_lock_routes(app)
        app.config["TESTING"] = True
        return app.test_client()

    def test_post_lock_passes_holder_type_and_ttl_through(self, client):
        resp = client.post(
            "/lock",
            json={"user": "alice", "holder_type": "ci", "ttl_seconds": 42},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["holder_type"] == "ci"
        assert body["ttl_seconds"] == 42

    def test_legacy_payload_through_shim(self, client):
        resp = client.post("/lock", json={"user": "alice"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["holder_type"] == "user"
        assert body["ttl_seconds"] is None

    def test_heartbeat_route_registered(self, client):
        client.post("/lock", json={"user": "alice", "holder_type": "ci"})
        resp = client.post("/lock/heartbeat", json={"user": "alice"})
        assert resp.status_code == 200

    def test_unlock_route_registered(self, client):
        client.post("/lock", json={"user": "alice"})
        resp = client.post("/unlock", json={"user": "alice"})
        assert resp.status_code == 200

    # Regression: non-dict JSON used to crash with 500 because downstream
    # handlers do data.get(...) / data['holder_type']. Each lock-route
    # POST must now reject non-object bodies with 400.
    @pytest.mark.parametrize("payload", [[], [1, 2], "hello", 42, True])
    @pytest.mark.parametrize("route", ["/lock", "/lock/heartbeat", "/unlock"])
    def test_non_dict_body_returns_400(self, client, route, payload):
        resp = client.post(route, json=payload)
        assert resp.status_code == 400
        assert "Expected a JSON object" in (resp.get_json() or {}).get("error", "")
