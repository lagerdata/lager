# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for cli/box_storage.py lock helpers added by the auto-lock work.

Covers:
- get_lock_holder(): dev fallback, LAGER_LOCK_HOLDER override, CI providers,
  matrix-safe uniqueness (pid + runner/host).
- acquire_box_lock(): acquired/already_ours states, 409 fail-fast,
  409 wait-then-retry, unreachable host returns ('unreachable', None).
- release_box_lock(): success / 403 / connection error never raises.
- heartbeat_box_lock(): 200 -> True, 404 -> False, transport error -> False.
- format_lock_user(): new ci:* formatting paths.
- default_lock_wait_seconds() CI vs dev defaults + env override.
- LockSession.dissolve(): the affordance `lager uninstall` uses once it has
  deleted the container serving the lock API.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
import requests

from cli import box_storage
from cli.context.ci_detection import CIEnvironment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code, json_body=None, text=''):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError('no body')
        return self._json


@pytest.fixture(autouse=True)
def _clear_lock_env(monkeypatch):
    for key in (
        'LAGER_LOCK_HOLDER', 'LAGER_USER', 'LAGER_LOCK_WAIT',
        'LAGER_LOCK_TTL', 'LAGER_LOCK_HEARTBEAT', 'CI', 'GITHUB_RUN_ID',
        'DRONE', 'GITLAB_CI', 'BITBUCKET_BUILD_NUMBER', 'JENKINS_URL',
        'CI_SERVER_NAME', 'BUILD_TAG',
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# get_lock_holder
# ---------------------------------------------------------------------------


class TestGetLockHolder:
    def test_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv('LAGER_LOCK_HOLDER', 'override:abc')
        assert box_storage.get_lock_holder() == 'override:abc'

    def test_dev_fallback_is_lager_user(self, monkeypatch):
        monkeypatch.setattr(box_storage, 'get_lager_user', lambda: 'alice')
        # No CI env -> get_ci_environment returns HOST
        assert box_storage.get_lock_holder() == 'alice'

    def test_github_holder_includes_run_attempt_job_runner_pid(self, monkeypatch):
        monkeypatch.setenv('CI', 'true')
        monkeypatch.setenv('GITHUB_RUN_ID', '999')
        monkeypatch.setenv('GITHUB_REPOSITORY', 'lager/lager')
        monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
        monkeypatch.setenv('GITHUB_JOB', 'integration')
        monkeypatch.setenv('RUNNER_NAME', 'bench-3')
        monkeypatch.setattr(os, 'getpid', lambda: 42)
        h = box_storage.get_lock_holder()
        assert h == 'ci:github:lager/lager#999-2/integration@bench-3:42'

    def test_drone_holder(self, monkeypatch):
        monkeypatch.setenv('CI', 'true')
        monkeypatch.setenv('DRONE', 'true')
        monkeypatch.setenv('DRONE_REPO', 'org/proj')
        monkeypatch.setenv('DRONE_BUILD_NUMBER', '15')
        monkeypatch.setattr(os, 'getpid', lambda: 5)
        monkeypatch.setattr('socket.gethostname', lambda: 'runner-7')
        assert box_storage.get_lock_holder() == 'ci:drone:org/proj#15:5@runner-7'

    def test_generic_ci_fallback(self, monkeypatch):
        monkeypatch.setenv('CI', 'true')
        monkeypatch.setattr(os, 'getpid', lambda: 11)
        monkeypatch.setattr('socket.gethostname', lambda: 'box-99')
        assert box_storage.get_lock_holder() == 'ci:generic:box-99:11'

    def test_matrix_safety_pid_differentiates(self, monkeypatch):
        # Two "processes" with otherwise-identical GitHub matrix env still
        # get distinct holders because pid differs.
        monkeypatch.setenv('CI', 'true')
        monkeypatch.setenv('GITHUB_RUN_ID', '1')
        monkeypatch.setenv('GITHUB_REPOSITORY', 'r')
        monkeypatch.setenv('GITHUB_JOB', 'j')
        monkeypatch.setenv('RUNNER_NAME', 'rn')

        monkeypatch.setattr(os, 'getpid', lambda: 100)
        h1 = box_storage.get_lock_holder()
        monkeypatch.setattr(os, 'getpid', lambda: 200)
        h2 = box_storage.get_lock_holder()
        assert h1 != h2


# ---------------------------------------------------------------------------
# format_lock_user (new CI cases)
# ---------------------------------------------------------------------------


class TestFormatLockUserNew:
    def test_github_human_readable(self):
        out = box_storage.format_lock_user(
            'ci:github:lager/lager#999-2/integration@bench-3:42'
        )
        assert out == 'github lager/lager run 999 job integration on bench-3'

    def test_drone_human_readable(self):
        out = box_storage.format_lock_user('ci:drone:org/proj#15:5@runner-7')
        assert out == 'drone org/proj build 15'

    def test_generic_human_readable(self):
        out = box_storage.format_lock_user('ci:generic:box-99:11')
        assert out == 'ci on box-99'

    def test_malformed_github_holder_falls_back_to_raw(self):
        # partition() never raises, so malformed strings used to render as
        # 'github  run ' garbage instead of hitting the fallback.
        for raw in ('ci:github:weird', 'ci:github:#123/job@r:9', 'ci:github:repo#'):
            assert box_storage.format_lock_user(raw) == raw

    def test_unknown_provider_passes_through(self):
        out = box_storage.format_lock_user('ci:mystery:whatever')
        assert out == 'ci:mystery:whatever'

    def test_reservation_prefix_still_works(self):
        out = box_storage.format_lock_user('dashboard:abc:user@example.com')
        assert out == 'user@example.com'


# ---------------------------------------------------------------------------
# default_lock_wait_seconds
# ---------------------------------------------------------------------------


class TestDefaultLockWaitSeconds:
    def test_dev_default_is_zero(self, monkeypatch):
        monkeypatch.setattr(
            'cli.context.ci_detection.get_ci_environment',
            lambda: CIEnvironment.HOST,
        )
        assert box_storage.default_lock_wait_seconds() == 0

    def test_ci_default_is_1800(self, monkeypatch):
        monkeypatch.setattr(
            'cli.context.ci_detection.get_ci_environment',
            lambda: CIEnvironment.GITHUB,
        )
        assert box_storage.default_lock_wait_seconds() == 1800

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv('LAGER_LOCK_WAIT', '42')
        assert box_storage.default_lock_wait_seconds() == 42

    def test_env_garbage_falls_back_to_dev(self, monkeypatch):
        monkeypatch.setenv('LAGER_LOCK_WAIT', 'not-an-int')
        assert box_storage.default_lock_wait_seconds() == 0


# ---------------------------------------------------------------------------
# default_auto_holder_type
# ---------------------------------------------------------------------------


class TestDefaultAutoHolderType:
    """Shared by `lager python` and the admin commands: 'ci' under any CI
    provider, 'ephemeral' on a dev machine. (Regression: `lager python`
    used to tag interactive dev runs as 'ci'.)"""

    def test_dev_is_ephemeral(self):
        assert box_storage.default_auto_holder_type() == 'ephemeral'

    def test_ci_is_ci(self, monkeypatch):
        monkeypatch.setenv('CI', 'true')
        monkeypatch.setenv('GITHUB_RUN_ID', '999')
        assert box_storage.default_auto_holder_type() == 'ci'


# ---------------------------------------------------------------------------
# acquire_box_lock
# ---------------------------------------------------------------------------


class TestAcquireBoxLock:
    @pytest.fixture(autouse=True)
    def _box_unlocked_on_get(self, monkeypatch):
        # acquire_box_lock now GETs /lock before POSTing (so a pre-existing
        # lock of ours is never re-acquired). Default every test to "box is
        # unlocked"; tests for the already_ours path override this.
        monkeypatch.setattr(
            requests, 'get',
            lambda *a, **k: _FakeResp(200, {'locked': False}),
        )

    def test_preexisting_own_lock_returns_already_ours_without_post(self, monkeypatch):
        # The guarantee: a lock we already hold (e.g. `lager boxes lock`)
        # must not even be POSTed to — a re-acquire would let the server
        # rewrite holder_type/ttl, and old servers would misreport it as
        # freshly acquired (and we'd release it on exit).
        monkeypatch.setattr(
            requests, 'get',
            lambda *a, **k: _FakeResp(200, {
                'locked': True, 'user': 'alice',
                'holder_type': 'user', 'ttl_seconds': None,
            }),
        )

        def no_post(*a, **k):
            raise AssertionError('must not POST /lock over our own lock')

        monkeypatch.setattr(requests, 'post', no_post)
        state, data = box_storage.acquire_box_lock(
            '10.0.0.1', 'lab-box', 'alice', wait_seconds=0,
        )
        assert state == 'already_ours'
        assert data['holder_type'] == 'user'

    def test_old_server_response_without_previous_user_is_acquired(self, monkeypatch):
        # Old servers don't echo previous_user at all. Since the GET above
        # said "unlocked", a 200 here means we genuinely created the lock.
        responses = iter([_FakeResp(200, {'locked': True, 'user': 'alice'})])
        monkeypatch.setattr(requests, 'post', lambda *a, **k: next(responses))
        state, _ = box_storage.acquire_box_lock(
            '10.0.0.1', 'lab-box', 'alice', wait_seconds=0,
        )
        assert state == 'acquired'

    def test_get_transport_error_falls_through_to_post(self, monkeypatch):
        def get_boom(*a, **k):
            raise requests.exceptions.ConnectionError('nope')

        monkeypatch.setattr(requests, 'get', get_boom)
        monkeypatch.setattr(
            requests, 'post',
            lambda *a, **k: _FakeResp(200, {'locked': True, 'user': 'alice', 'previous_user': None}),
        )
        state, _ = box_storage.acquire_box_lock(
            '10.0.0.1', 'lab-box', 'alice', wait_seconds=0,
        )
        assert state == 'acquired'

    def test_acquired_when_previous_user_differs(self, monkeypatch):
        responses = iter([_FakeResp(200, {'locked': True, 'user': 'alice', 'previous_user': None})])
        monkeypatch.setattr(requests, 'post', lambda *a, **k: next(responses))
        state, data = box_storage.acquire_box_lock(
            '10.0.0.1', 'lab-box', 'alice', wait_seconds=0,
        )
        assert state == 'acquired'
        assert data['user'] == 'alice'

    def test_already_ours_when_previous_user_matches(self, monkeypatch):
        responses = iter([_FakeResp(200, {'locked': True, 'user': 'alice', 'previous_user': 'alice'})])
        monkeypatch.setattr(requests, 'post', lambda *a, **k: next(responses))
        state, _ = box_storage.acquire_box_lock(
            '10.0.0.1', 'lab-box', 'alice', wait_seconds=0,
        )
        assert state == 'already_ours'

    def test_409_fail_fast_exits_1(self, monkeypatch):
        monkeypatch.setattr(requests, 'post', lambda *a, **k: _FakeResp(
            409, {'error': 'locked', 'lock': {'user': 'bob'}},
        ))
        with pytest.raises(SystemExit) as exc:
            box_storage.acquire_box_lock(
                '10.0.0.1', 'lab-box', 'alice', wait_seconds=0, quiet=True,
            )
        assert exc.value.code == 1

    def test_409_then_200_with_wait(self, monkeypatch):
        responses = iter([
            _FakeResp(409, {'lock': {'user': 'bob'}}),
            _FakeResp(409, {'lock': {'user': 'bob'}}),
            _FakeResp(200, {'locked': True, 'user': 'alice', 'previous_user': None}),
        ])
        monkeypatch.setattr(requests, 'post', lambda *a, **k: next(responses))
        monkeypatch.setattr('time.sleep', lambda _s: None)
        state, _ = box_storage.acquire_box_lock(
            '10.0.0.1', 'lab-box', 'alice',
            wait_seconds=60, poll=0.01, quiet=True,
        )
        assert state == 'acquired'

    def test_unreachable_returns_sentinel(self, monkeypatch):
        def boom(*a, **k):
            raise requests.exceptions.ConnectionError('nope')
        monkeypatch.setattr(requests, 'post', boom)
        state, data = box_storage.acquire_box_lock(
            '10.0.0.1', 'lab-box', 'alice', wait_seconds=0, quiet=True,
        )
        assert state == 'unreachable'
        assert data is None

    def test_sends_holder_type_and_ttl_in_payload(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured['url'] = url
            captured['json'] = json
            return _FakeResp(200, {'previous_user': None})

        monkeypatch.setattr(requests, 'post', fake_post)
        box_storage.acquire_box_lock(
            '10.0.0.1', 'lab-box', 'alice',
            holder_type='ephemeral', ttl_seconds=None, wait_seconds=0,
        )
        assert captured['url'] == 'http://10.0.0.1:9000/lock'
        assert captured['json']['user'] == 'alice'
        assert captured['json']['holder_type'] == 'ephemeral'
        assert captured['json']['ttl_seconds'] is None


# ---------------------------------------------------------------------------
# release_box_lock & heartbeat_box_lock
# ---------------------------------------------------------------------------


class TestReleaseBoxLock:
    def test_success_returns_true(self, monkeypatch):
        monkeypatch.setattr(requests, 'post', lambda *a, **k: _FakeResp(200))
        assert box_storage.release_box_lock('10.0.0.1', 'alice') is True

    def test_403_returns_false_but_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(requests, 'post', lambda *a, **k: _FakeResp(403, {'error': 'no'}))
        assert box_storage.release_box_lock('10.0.0.1', 'alice') is False

    def test_connection_error_returns_false(self, monkeypatch):
        def boom(*a, **k):
            raise requests.exceptions.ConnectionError('nope')
        monkeypatch.setattr(requests, 'post', boom)
        assert box_storage.release_box_lock('10.0.0.1', 'alice') is False

    def test_hits_unlock_endpoint(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured['url'] = url
            return _FakeResp(200)

        monkeypatch.setattr(requests, 'post', fake_post)
        box_storage.release_box_lock('10.0.0.1', 'alice')
        assert captured['url'] == 'http://10.0.0.1:9000/unlock'


class TestHeartbeatBoxLock:
    def test_200_returns_true(self, monkeypatch):
        monkeypatch.setattr(requests, 'post', lambda *a, **k: _FakeResp(200))
        assert box_storage.heartbeat_box_lock('10.0.0.1', 'alice') is True

    def test_404_returns_false(self, monkeypatch):
        monkeypatch.setattr(requests, 'post', lambda *a, **k: _FakeResp(404))
        assert box_storage.heartbeat_box_lock('10.0.0.1', 'alice') is False

    def test_transport_error_returns_false(self, monkeypatch):
        def boom(*a, **k):
            raise requests.exceptions.Timeout('slow')
        monkeypatch.setattr(requests, 'post', boom)
        assert box_storage.heartbeat_box_lock('10.0.0.1', 'alice') is False

    def test_hits_heartbeat_endpoint(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured['url'] = url
            captured['json'] = json
            return _FakeResp(200)

        monkeypatch.setattr(requests, 'post', fake_post)
        box_storage.heartbeat_box_lock('10.0.0.1', 'alice')
        assert captured['url'] == 'http://10.0.0.1:9000/lock/heartbeat'
        assert captured['json'] == {'user': 'alice'}


# ---------------------------------------------------------------------------
# auto_lock_around_command  (context manager used by install/uninstall/
# install-wheel) + auto_lock_acquire_for_command (imperative variant used
# by update).
# ---------------------------------------------------------------------------


class TestAutoLockAroundCommand:
    """Context-manager helper. Verifies the full happy-path lifecycle
    (acquire on enter, heartbeat starts, release on exit) plus a few
    edge cases (LAGER_AUTO_LOCK_DISABLE, already_ours, unreachable)."""

    def test_acquires_on_enter_and_releases_on_exit(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: (calls.append(('acquire', a, k)) or ('acquired', {})),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock',
            lambda *a, **k: calls.append(('release', a, k)) or True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )
        # Skip the heartbeat thread for determinism.
        monkeypatch.setattr(
            box_storage, 'HeartbeatThread',
            lambda *a, **k: mock.Mock(start=mock.Mock(), stop=mock.Mock()),
        )

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'install',
        ) as (holder, state):
            assert holder == 'test-holder'
            assert state == 'acquired'

        kinds = [c[0] for c in calls]
        assert kinds == ['acquire', 'release']

    def test_release_fires_on_exception(self, monkeypatch):
        released = []
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: ('acquired', {}),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock',
            lambda *a, **k: released.append(a) or True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )
        monkeypatch.setattr(
            box_storage, 'HeartbeatThread',
            lambda *a, **k: mock.Mock(start=mock.Mock(), stop=mock.Mock()),
        )

        with pytest.raises(RuntimeError):
            with box_storage.auto_lock_around_command(
                '10.0.0.1', 'lab-box', 'install',
            ):
                raise RuntimeError("oops")

        assert len(released) == 1, "release must run on exception unwind"

    def test_release_fires_on_systemexit(self, monkeypatch):
        # `ctx.exit(1)` raises SystemExit inside the with-block. The lock
        # release must still run.
        released = []
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: ('acquired', {}),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock',
            lambda *a, **k: released.append(a) or True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )
        monkeypatch.setattr(
            box_storage, 'HeartbeatThread',
            lambda *a, **k: mock.Mock(start=mock.Mock(), stop=mock.Mock()),
        )

        with pytest.raises(SystemExit):
            with box_storage.auto_lock_around_command(
                '10.0.0.1', 'lab-box', 'install',
            ):
                raise SystemExit(1)
        assert len(released) == 1

    def test_disabled_via_env_yields_disabled_state(self, monkeypatch):
        # The LAGER_AUTO_LOCK_DISABLE escape hatch must skip the acquire
        # entirely and yield state="disabled".
        monkeypatch.setenv('LAGER_AUTO_LOCK_DISABLE', '1')

        def boom(*a, **k):
            raise AssertionError("acquire must not be called when disabled")

        monkeypatch.setattr(box_storage, 'acquire_box_lock', boom)
        monkeypatch.setattr(box_storage, 'release_box_lock', boom)
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'install',
        ) as (_holder, state):
            assert state == 'disabled'

    def test_already_ours_does_not_release(self, monkeypatch):
        # When a user lock from `lager boxes lock` already exists with the
        # SAME holder, acquire_box_lock returns ("already_ours", ...) and
        # we must NOT release on exit — the user lock has to survive.
        released = []
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: ('already_ours', {}),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock',
            lambda *a, **k: released.append(a) or True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'install',
        ) as (_holder, state):
            assert state == 'already_ours'

        assert released == [], "must not release a pre-existing user lock"

    def test_atexit_handler_unregistered_after_release(self, monkeypatch):
        # One process can take many locks (test suites, scripts); spent
        # release handlers must not accumulate in atexit.
        import atexit as real_atexit
        unregistered = []
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock', lambda *a, **k: ('acquired', {}),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock', lambda *a, **k: True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )
        monkeypatch.setattr(
            box_storage, 'HeartbeatThread',
            lambda *a, **k: mock.Mock(start=mock.Mock(), stop=mock.Mock()),
        )
        monkeypatch.setattr(
            real_atexit, 'unregister', lambda fn: unregistered.append(fn),
        )

        with box_storage.auto_lock_around_command('10.0.0.1', 'lab-box', 'install'):
            pass
        assert len(unregistered) == 1

        release = box_storage.auto_lock_acquire_for_command(
            '10.0.0.1', 'lab-box', 'update',
        )
        release()
        assert len(unregistered) == 2

    def test_already_ours_with_ttl_heartbeats_but_never_releases(self, monkeypatch):
        # Resuming a leftover ephemeral lock (crashed run): keep it alive
        # with a heartbeat so it can't TTL-expire mid-command, but still
        # never release it.
        released = []
        hb = mock.Mock(start=mock.Mock(), stop=mock.Mock())
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: ('already_ours', {'ttl_seconds': 1800}),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock',
            lambda *a, **k: released.append(a) or True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )
        monkeypatch.setattr(box_storage, 'HeartbeatThread', lambda *a, **k: hb)

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'install',
        ):
            pass

        hb.start.assert_called_once()
        hb.stop.assert_called_once()
        assert released == []

    def test_already_ours_eternal_lock_gets_no_heartbeat(self, monkeypatch):
        # A pre-existing user lock (ttl null) needs no keep-alive.
        def no_heartbeat(*a, **k):
            raise AssertionError('must not heartbeat an eternal user lock')

        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: ('already_ours', {'holder_type': 'user', 'ttl_seconds': None}),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock', lambda *a, **k: True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )
        monkeypatch.setattr(box_storage, 'HeartbeatThread', no_heartbeat)

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'install',
        ):
            pass

    def test_unreachable_box_does_not_release(self, monkeypatch):
        # If the box is unreachable, acquire_box_lock returns
        # ("unreachable", None) — we never held the lock so don't try
        # to release.
        released = []
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: ('unreachable', None),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock',
            lambda *a, **k: released.append(a) or True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'install',
        ):
            pass

        assert released == []

    def test_passes_ci_holder_type_when_in_ci(self, monkeypatch):
        # holder_type defaults to 'ci' when running under any CI provider.
        captured = {}
        monkeypatch.setenv('CI', 'true')
        monkeypatch.setenv('GITHUB_RUN_ID', '999')

        def fake_acquire(*args, **kwargs):
            captured.update(kwargs)
            return ('acquired', {})

        monkeypatch.setattr(box_storage, 'acquire_box_lock', fake_acquire)
        monkeypatch.setattr(
            box_storage, 'release_box_lock', lambda *a, **k: True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'gh:run-999',
        )
        monkeypatch.setattr(
            box_storage, 'HeartbeatThread',
            lambda *a, **k: mock.Mock(start=mock.Mock(), stop=mock.Mock()),
        )

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'install',
        ):
            pass

        assert captured['holder_type'] == 'ci'


class TestLockSessionDissolve:
    """`lager uninstall` removes the lager container — the process serving
    the :9000 lock API its own lock lives in — partway through its run.
    dissolve() is how it says so: stop heartbeating, skip the release.

    Every OTHER caller (install, install-wheel, lager python) must be
    unaffected, which the untouched tests above assert by never calling it.
    """

    @staticmethod
    def _patch_lock(monkeypatch, heartbeat, released, state='acquired', lock_data=None):
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: (state, lock_data if lock_data is not None else {}),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock',
            lambda *a, **k: released.append(a) or True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )
        monkeypatch.setattr(box_storage, 'HeartbeatThread', lambda *a, **k: heartbeat)

    def test_dissolve_stops_heartbeat_and_skips_release(self, monkeypatch):
        released = []
        hb = mock.Mock(start=mock.Mock(), stop=mock.Mock())
        self._patch_lock(monkeypatch, hb, released)

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'uninstall',
        ) as session:
            hb.start.assert_called_once()
            session.dissolve()
            # Stopped INSIDE the block: the point is that no heartbeat is
            # attempted over the rest of the command, not merely that the
            # thread is tidied up at exit (the finally already did that).
            assert hb.stop.called, 'dissolve must stop the heartbeat immediately'

        assert released == [], 'must not POST a release to a deleted server'

    def test_dissolve_is_idempotent(self, monkeypatch):
        released = []
        hb = mock.Mock(start=mock.Mock(), stop=mock.Mock())
        self._patch_lock(monkeypatch, hb, released)

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'uninstall',
        ) as session:
            session.dissolve()
            session.dissolve()
            session.dissolve()

        assert released == []

    def test_dissolve_unregisters_the_atexit_fallback(self, monkeypatch):
        # The atexit handler is the release path for signals and os._exit.
        # Left registered, it would fire the doomed POST at interpreter
        # shutdown — exactly what dissolve exists to avoid.
        import atexit as real_atexit
        unregistered = []
        released = []
        hb = mock.Mock(start=mock.Mock(), stop=mock.Mock())
        self._patch_lock(monkeypatch, hb, released)
        monkeypatch.setattr(
            real_atexit, 'unregister', lambda fn: unregistered.append(fn),
        )

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'uninstall',
        ) as session:
            session.dissolve()
            assert len(unregistered) == 1

        # The finally's _safe_release is a no-op now, so it must not
        # unregister a second time.
        assert len(unregistered) == 1

    def test_dissolve_stops_heartbeat_of_a_resumed_lock(self, monkeypatch):
        # Resumed ephemeral lock (state 'already_ours' with a TTL) gets a
        # heartbeat but is never released. Uninstall can still be the
        # command that deletes the server, so dissolve must silence it.
        released = []
        hb = mock.Mock(start=mock.Mock(), stop=mock.Mock())
        self._patch_lock(
            monkeypatch, hb, released,
            state='already_ours', lock_data={'ttl_seconds': 1800},
        )

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'uninstall',
        ) as session:
            session.dissolve()
            assert hb.stop.called

        assert released == []

    def test_dissolve_is_a_noop_under_the_disable_escape_hatch(self, monkeypatch):
        # LAGER_AUTO_LOCK_DISABLE skips the acquire entirely; the yielded
        # handle must still answer dissolve() rather than AttributeError.
        monkeypatch.setenv('LAGER_AUTO_LOCK_DISABLE', '1')

        def boom(*a, **k):
            raise AssertionError('no lock is held when disabled')

        monkeypatch.setattr(box_storage, 'acquire_box_lock', boom)
        monkeypatch.setattr(box_storage, 'release_box_lock', boom)
        monkeypatch.setattr(box_storage, 'get_lock_holder', lambda: 'test-holder')

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'uninstall',
        ) as session:
            assert session.state == 'disabled'
            session.dissolve()

    def test_session_still_unpacks_as_holder_state(self, monkeypatch):
        # Guards the callers (and tests) written against the old tuple
        # yield: the handle grew a method, it did not change shape.
        released = []
        hb = mock.Mock(start=mock.Mock(), stop=mock.Mock())
        self._patch_lock(monkeypatch, hb, released)

        with box_storage.auto_lock_around_command(
            '10.0.0.1', 'lab-box', 'install',
        ) as (holder, state):
            assert (holder, state) == ('test-holder', 'acquired')

        assert len(released) == 1, 'a session nobody dissolved still releases'


class TestAutoLockAcquireForCommand:
    """Imperative variant used by update.py. Same lifecycle guarantees as
    the context manager, but the release callable is returned to the
    caller and we register an atexit hook as a fallback."""

    def test_returns_callable_that_releases(self, monkeypatch):
        released = []
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: ('acquired', {}),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock',
            lambda *a, **k: released.append(a) or True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )
        monkeypatch.setattr(
            box_storage, 'HeartbeatThread',
            lambda *a, **k: mock.Mock(start=mock.Mock(), stop=mock.Mock()),
        )

        release = box_storage.auto_lock_acquire_for_command(
            '10.0.0.1', 'lab-box', 'update',
        )
        assert release.state == 'acquired'
        release()
        assert len(released) == 1

    def test_release_is_idempotent(self, monkeypatch):
        # Long-running commands often have multiple cleanup paths
        # (explicit release + atexit). Calling release() more than
        # once must NOT double-release.
        released = []
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: ('acquired', {}),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock',
            lambda *a, **k: released.append(a) or True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )
        monkeypatch.setattr(
            box_storage, 'HeartbeatThread',
            lambda *a, **k: mock.Mock(start=mock.Mock(), stop=mock.Mock()),
        )

        release = box_storage.auto_lock_acquire_for_command(
            '10.0.0.1', 'lab-box', 'update',
        )
        release()
        release()
        release()
        assert len(released) == 1, "release must be idempotent"

    def test_disabled_returns_noop(self, monkeypatch):
        monkeypatch.setenv('LAGER_AUTO_LOCK_DISABLE', '1')

        def boom(*a, **k):
            raise AssertionError("acquire must not be called when disabled")

        monkeypatch.setattr(box_storage, 'acquire_box_lock', boom)
        monkeypatch.setattr(box_storage, 'release_box_lock', boom)

        release = box_storage.auto_lock_acquire_for_command(
            '10.0.0.1', 'lab-box', 'update',
        )
        assert release.state == 'disabled'
        release()  # no-op; must not raise

    def test_already_ours_with_ttl_heartbeats_but_never_releases(self, monkeypatch):
        released = []
        hb = mock.Mock(start=mock.Mock(), stop=mock.Mock())
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: ('already_ours', {'ttl_seconds': 1800}),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock',
            lambda *a, **k: released.append(a) or True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )
        monkeypatch.setattr(box_storage, 'HeartbeatThread', lambda *a, **k: hb)

        release = box_storage.auto_lock_acquire_for_command(
            '10.0.0.1', 'lab-box', 'update',
        )
        assert release.state == 'already_ours'
        hb.start.assert_called_once()
        release()
        hb.stop.assert_called_once()
        assert released == [], "must not release a resumed lock"

    def test_already_ours_eternal_lock_gets_no_heartbeat(self, monkeypatch):
        def no_heartbeat(*a, **k):
            raise AssertionError('must not heartbeat an eternal user lock')

        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **k: ('already_ours', {'holder_type': 'user', 'ttl_seconds': None}),
        )
        monkeypatch.setattr(
            box_storage, 'release_box_lock', lambda *a, **k: True,
        )
        monkeypatch.setattr(
            box_storage, 'get_lock_holder', lambda: 'test-holder',
        )
        monkeypatch.setattr(box_storage, 'HeartbeatThread', no_heartbeat)

        release = box_storage.auto_lock_acquire_for_command(
            '10.0.0.1', 'lab-box', 'update',
        )
        release()  # stops nothing, releases nothing; must not raise


# ---------------------------------------------------------------------------
# HeartbeatThread warning threshold
# ---------------------------------------------------------------------------


def _drive(thread, attempts):
    """Run ``thread``'s loop for exactly ``attempts`` renewal attempts.

    Patches the stop event's wait so no wall-clock time passes: it returns
    False (keep going) until the budget is spent, then True (stop).
    """
    seen = {'n': 0}

    def fast_wait(_seconds):
        seen['n'] += 1
        return seen['n'] > attempts

    with mock.patch.object(thread._stop_event, 'wait', side_effect=fast_wait):
        thread.start()
        thread.join(timeout=5.0)
    assert not thread.is_alive()


class TestHeartbeatWarningThreshold:
    """The warning has to mean something.

    Interval is 60s against a 1800s TTL, so one failed renewal has spent
    1/30th of the budget. Warning on it made `lager install` — which replaces
    the container serving the lock API, guaranteeing minutes of failures —
    print a scary line on every successful run.
    """

    def test_a_short_outage_says_nothing(self, monkeypatch, capsys):
        # 5 minutes of failures against a 30 minute TTL: 25 minutes of margin
        # left. This is the `lager install` container-rebuild window.
        monkeypatch.setattr(box_storage, 'heartbeat_box_lock', lambda *a, **k: False)
        t = box_storage.HeartbeatThread(
            '10.0.0.1', 'alice', 60, warn_label='install lock heartbeat',
            ttl_seconds=1800,
        )
        _drive(t, attempts=5)
        assert capsys.readouterr().err == ''

    def test_it_warns_once_the_lock_is_actually_at_risk(self, monkeypatch, capsys):
        monkeypatch.setattr(box_storage, 'heartbeat_box_lock', lambda *a, **k: False)
        t = box_storage.HeartbeatThread(
            '10.0.0.1', 'alice', 60, warn_label='install lock heartbeat',
            ttl_seconds=1800,
        )
        _drive(t, attempts=15)  # 15 * 60s == 900s == half of 1800
        err = capsys.readouterr().err
        assert 'install lock heartbeat' in err
        # The elapsed time is the number that tells you whether to worry, so
        # it has to be in the message rather than "relying on server TTL".
        assert '15m' in err and '30m' in err

    def test_it_warns_only_once_per_outage(self, monkeypatch, capsys):
        monkeypatch.setattr(box_storage, 'heartbeat_box_lock', lambda *a, **k: False)
        t = box_storage.HeartbeatThread(
            '10.0.0.1', 'alice', 60, ttl_seconds=1800,
        )
        _drive(t, attempts=40)
        assert capsys.readouterr().err.count('Warning:') == 1

    def test_a_successful_renewal_rearms_the_warning(self, monkeypatch, capsys):
        # Fail long enough to warn, recover, then fail that long again. A box
        # that keeps dropping out should be reported each time it gets close,
        # not silenced for the rest of the process.
        results = [False] * 15 + [True] + [False] * 15
        it = iter(results)
        monkeypatch.setattr(
            box_storage, 'heartbeat_box_lock', lambda *a, **k: next(it, False),
        )
        t = box_storage.HeartbeatThread(
            '10.0.0.1', 'alice', 60, ttl_seconds=1800,
        )
        _drive(t, attempts=len(results))
        assert capsys.readouterr().err.count('Warning:') == 2

    def test_an_eternal_lock_falls_back_to_a_failure_count(self, monkeypatch, capsys):
        # ttl_seconds=None (a --detach run) cannot expire, so there is no
        # deadline to measure against — but a box that has been unreachable
        # for five straight attempts is still worth saying out loud.
        monkeypatch.setattr(box_storage, 'heartbeat_box_lock', lambda *a, **k: False)

        quiet = box_storage.HeartbeatThread('10.0.0.1', 'alice', 60, ttl_seconds=None)
        _drive(quiet, attempts=4)
        assert capsys.readouterr().err == ''

        loud = box_storage.HeartbeatThread('10.0.0.1', 'alice', 60, ttl_seconds=None)
        _drive(loud, attempts=5)
        err = capsys.readouterr().err
        assert 'may be unreachable' in err
        assert 'will expire' not in err  # it cannot; don't claim otherwise

    def test_failures_never_stop_the_thread(self, monkeypatch):
        calls = []

        def flaky(*_a, **_k):
            calls.append(1)
            raise RuntimeError('network down')

        monkeypatch.setattr(box_storage, 'heartbeat_box_lock', flaky)
        t = box_storage.HeartbeatThread('10.0.0.1', 'alice', 60, ttl_seconds=1800)
        _drive(t, attempts=20)
        assert len(calls) == 20


class TestFormatDuration:
    @pytest.mark.parametrize('seconds,expected', [
        (0, '0s'),
        (45, '45s'),
        (60, '1m'),
        (90, '1m30s'),
        (900, '15m'),
        (1800, '30m'),
        (3600, '1h'),
        (5400, '1h30m'),
    ])
    def test_shortest_honest_unit(self, seconds, expected):
        assert box_storage._format_duration(seconds) == expected
