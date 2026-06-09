# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for box/lager/http_handlers/lock_handler.py.

Exercises the server-side lock state machine via a Flask test client with
``LOCK_FILE`` / ``LOCK_FILE_GUARD`` redirected to a tmpdir so we never
touch ``/etc/lager``. Covers:

- POST /lock acquire / re-acquire / collision
- POST /lock with legacy payload (no holder_type, no ttl_seconds)
- POST /lock with explicit user / ephemeral holder types
- POST /lock/heartbeat refresh & permission
- TTL-based auto-reap (and the null-TTL eternal case)
- POST /unlock incl. --force
- Atomic write durability across reads
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask


_LOCK_HANDLER_PY = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "..", "..",
        "box", "lager", "http_handlers", "lock_handler.py",
    )
)


def _load_lock_handler_module():
    """Load lock_handler.py without going through ``import lager`` (which would
    pull in box-only deps like pyvisa). Mirrors the pattern used by
    ``test_box_config.py``."""
    name = "lock_handler_under_test"
    spec = importlib.util.spec_from_file_location(name, _LOCK_HANDLER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """Flask app with the lock routes registered and disk paths redirected."""
    lock_handler = _load_lock_handler_module()

    lock_file = tmp_path / "lock.json"
    guard_file = tmp_path / "lock.json.flock"
    monkeypatch.setattr(lock_handler, "LOCK_FILE", str(lock_file))
    monkeypatch.setattr(lock_handler, "LOCK_FILE_GUARD", str(guard_file))

    flask_app = Flask(__name__)
    lock_handler.register_lock_routes(flask_app)
    flask_app.config["TESTING"] = True
    flask_app.config["_lock_file"] = lock_file
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


def _post_lock(client, **payload):
    return client.post("/lock", json=payload)


class TestAcquire:
    def test_legacy_payload_defaults_to_eternal_user_lock(self, client, app):
        resp = _post_lock(client, user="alice")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["locked"] is True
        assert body["user"] == "alice"
        assert body["holder_type"] == "user"
        assert body["ttl_seconds"] is None
        assert body["previous_user"] is None  # we acquired

        on_disk = json.loads(app.config["_lock_file"].read_text())
        assert on_disk["holder_type"] == "user"
        assert on_disk["ttl_seconds"] is None

    def test_explicit_user_lock(self, client):
        resp = _post_lock(client, user="alice", holder_type="user", ttl_seconds=None)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["holder_type"] == "user"
        assert body["ttl_seconds"] is None

    def test_ephemeral_lock_defaults_ttl_to_1800(self, client):
        resp = _post_lock(client, user="ci:github:repo#1-1/job@runner:42", holder_type="ci")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["holder_type"] == "ci"
        assert body["ttl_seconds"] == 1800

    def test_reacquire_same_holder_returns_already_ours(self, client):
        _post_lock(client, user="alice")
        resp = _post_lock(client, user="alice")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["previous_user"] == "alice"

    def test_collision_returns_409(self, client):
        _post_lock(client, user="alice")
        resp = _post_lock(client, user="bob")
        assert resp.status_code == 409
        body = resp.get_json()
        assert "locked by alice" in body["error"]
        assert body["lock"]["user"] == "alice"

    def test_missing_user_is_400(self, client):
        resp = client.post("/lock", json={})
        assert resp.status_code == 400

    def test_normalize_ttl_accepts_string_null(self, client):
        resp = _post_lock(client, user="alice", holder_type="user", ttl_seconds="null")
        assert resp.status_code == 200
        assert resp.get_json()["ttl_seconds"] is None

    def test_normalize_ttl_clamps_negative_to_one(self, client):
        resp = _post_lock(client, user="alice", holder_type="ci", ttl_seconds=-5)
        assert resp.status_code == 200
        assert resp.get_json()["ttl_seconds"] == 1


class TestHeartbeat:
    def test_heartbeat_refreshes_last_heartbeat(self, client):
        _post_lock(client, user="alice", holder_type="ci", ttl_seconds=1800)
        first = client.get("/lock").get_json()
        first_hb = first["last_heartbeat"]

        # Sleep one second so the ISO-formatted timestamp can differ.
        import time
        time.sleep(1.1)
        resp = client.post("/lock/heartbeat", json={"user": "alice"})
        assert resp.status_code == 200
        second = client.get("/lock").get_json()
        assert second["last_heartbeat"] >= first_hb
        assert second["last_heartbeat"] != first_hb

    def test_heartbeat_404_when_unlocked(self, client):
        resp = client.post("/lock/heartbeat", json={"user": "alice"})
        assert resp.status_code == 404

    def test_heartbeat_403_for_other_holder(self, client):
        _post_lock(client, user="alice", holder_type="ci", ttl_seconds=1800)
        resp = client.post("/lock/heartbeat", json={"user": "bob"})
        assert resp.status_code == 403

    def test_heartbeat_requires_user(self, client):
        resp = client.post("/lock/heartbeat", json={})
        assert resp.status_code == 400


class TestTTL:
    def _stale_heartbeat(self, app, seconds_ago):
        path = app.config["_lock_file"]
        data = json.loads(path.read_text())
        past = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["last_heartbeat"] = past
        path.write_text(json.dumps(data))

    def test_expired_lock_is_auto_reaped_on_read(self, client, app):
        _post_lock(client, user="alice", holder_type="ci", ttl_seconds=10)
        self._stale_heartbeat(app, seconds_ago=300)
        status = client.get("/lock").get_json()
        assert status == {"locked": False}
        assert not app.config["_lock_file"].exists()

    def test_eternal_lock_never_expires(self, client, app):
        _post_lock(client, user="alice", holder_type="user", ttl_seconds=None)
        self._stale_heartbeat(app, seconds_ago=99999)
        status = client.get("/lock").get_json()
        assert status["locked"] is True
        assert status["user"] == "alice"

    def test_expired_lock_lets_new_holder_acquire(self, client, app):
        _post_lock(client, user="alice", holder_type="ci", ttl_seconds=10)
        self._stale_heartbeat(app, seconds_ago=300)
        resp = _post_lock(client, user="bob", holder_type="ci")
        assert resp.status_code == 200
        assert resp.get_json()["user"] == "bob"


class TestUnlock:
    def test_unlock_own(self, client):
        _post_lock(client, user="alice")
        resp = client.post("/unlock", json={"user": "alice"})
        assert resp.status_code == 200
        assert resp.get_json()["locked"] is False

    def test_unlock_other_without_force_403(self, client):
        _post_lock(client, user="alice")
        resp = client.post("/unlock", json={"user": "bob"})
        assert resp.status_code == 403

    def test_unlock_other_with_force(self, client):
        _post_lock(client, user="alice")
        resp = client.post("/unlock", json={"user": "bob", "force": True})
        assert resp.status_code == 200

    def test_unlock_when_already_unlocked(self, client):
        resp = client.post("/unlock", json={"user": "alice"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["locked"] is False


class TestStatus:
    def test_unlocked_status(self, client):
        assert client.get("/lock").get_json() == {"locked": False}

    def test_locked_status_returns_lock_record(self, client):
        _post_lock(client, user="alice", holder_type="ci", ttl_seconds=300)
        body = client.get("/lock").get_json()
        assert body["locked"] is True
        assert body["user"] == "alice"
        assert body["ttl_seconds"] == 300
        assert "locked_at" in body
        assert "last_heartbeat" in body
