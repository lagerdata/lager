# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for box/lager/python/job_lock.py.

A detached run has no CLI left to hold its box lock: the client is answered and
goes away, which is the point. So the lock was acquired eternal
(``ttl_seconds: null``) and released by hand. Workable while the job runs, and
a trap when it does not -- a detached job that failed to start left the box
locked with nothing running on it.

The box now holds the lock for exactly as long as the job it launched. What
these pin is the blast radius of that, because "the box can release a lock" is
the part worth being careful about:

* it only ever touches the holder that came with the job, and
* it stops rather than fights when the lock stops being that holder's.

The safety comes from lock_state itself -- heartbeat 403s a foreign holder,
release refuses on a mismatch -- so these tests check that job_lock passes the
holder through faithfully and reacts to a refusal by giving up.
"""

import os
import sys
import types

import pytest
from unittest.mock import MagicMock


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted: str) -> None:
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core', 'pigpio',
    'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
]:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager import lock_state  # noqa: E402
from lager.python.job_lock import DetachedJobLock  # noqa: E402

HOLDER = 'benchuser@bench-1234'


class FakeLockState:
    """Records what job_lock asked of lock_state, and answers as told."""

    def __init__(self, heartbeat_code=200, release_code=200):
        self.heartbeats = []
        self.releases = []
        self.heartbeat_code = heartbeat_code
        self.release_code = release_code

    def heartbeat(self, user):
        self.heartbeats.append(user)
        return self.heartbeat_code, {'error': 'Box is locked by someone else'}

    def release(self, user, force=False):
        self.releases.append((user, force))
        return self.release_code, {'locked': False}


@pytest.fixture
def fake(monkeypatch):
    state = FakeLockState()
    monkeypatch.setattr(lock_state, 'heartbeat', state.heartbeat)
    monkeypatch.setattr(lock_state, 'release', state.release)
    return state


class TestItOnlyEverTouchesItsOwnHolder:

    def test_heartbeats_the_holder_it_was_given(self, fake):
        job_lock = DetachedJobLock(HOLDER, interval=0.01)
        job_lock.start()
        deadline_reached = _wait(lambda: len(fake.heartbeats) >= 2)
        job_lock.stop()

        assert deadline_reached, 'expected repeated heartbeats'
        assert set(fake.heartbeats) == {HOLDER}

    def test_releases_the_holder_it_was_given_without_forcing(self, fake):
        """force=False is what makes this safe.

        A forced release would clear whatever lock happened to be there,
        including a `lager boxes lock` reservation someone else was relying on.
        """
        job_lock = DetachedJobLock(HOLDER, interval=60)
        job_lock.start()
        job_lock.stop()

        assert fake.releases == [(HOLDER, False)]

    def test_a_release_refusal_is_not_escalated(self, monkeypatch):
        """403 means the lock is someone else's now. That is the end of it."""
        state = FakeLockState(release_code=403)
        monkeypatch.setattr(lock_state, 'release', state.release)

        DetachedJobLock(HOLDER, interval=60).stop()

        assert state.releases == [(HOLDER, False)]     # asked once, did not retry


class TestItStopsRatherThanFights:

    def test_stops_heartbeating_once_the_lock_is_no_longer_ours(self, monkeypatch):
        state = FakeLockState(heartbeat_code=403)
        monkeypatch.setattr(lock_state, 'heartbeat', state.heartbeat)
        monkeypatch.setattr(lock_state, 'release', state.release)

        job_lock = DetachedJobLock(HOLDER, interval=0.01)
        job_lock.start()
        _wait(lambda: len(state.heartbeats) >= 1)
        _wait(lambda: not job_lock._thread.is_alive())
        job_lock.stop()

        assert len(state.heartbeats) == 1, state.heartbeats

    def test_a_heartbeat_that_raises_ends_the_thread(self, monkeypatch):
        calls = []

        def exploding_heartbeat(user):
            calls.append(user)
            raise OSError('lock file vanished')

        monkeypatch.setattr(lock_state, 'heartbeat', exploding_heartbeat)
        monkeypatch.setattr(lock_state, 'release', lambda user, force=False: (200, {}))

        job_lock = DetachedJobLock(HOLDER, interval=0.01)
        job_lock.start()
        _wait(lambda: not job_lock._thread.is_alive())
        job_lock.stop()

        assert len(calls) == 1


class TestWithoutAHolder:
    """A run that did not auto-lock, a resumed reservation, or an older CLI.

    Callers do not branch on this -- start() and stop() are always safe to
    call -- so it has to be inert rather than merely harmless.
    """

    def test_never_heartbeats(self, fake):
        job_lock = DetachedJobLock(None, interval=0.01)
        job_lock.start()
        _wait(lambda: False, timeout=0.1)
        job_lock.stop()

        assert fake.heartbeats == []

    def test_never_releases(self, fake):
        DetachedJobLock(None).stop()
        assert fake.releases == []

    def test_starts_no_thread_at_all(self, fake):
        job_lock = DetachedJobLock('')
        job_lock.start()
        assert job_lock._thread is None


class TestLifecycle:

    def test_start_is_idempotent(self, fake):
        job_lock = DetachedJobLock(HOLDER, interval=60)
        job_lock.start()
        first = job_lock._thread
        job_lock.start()

        assert job_lock._thread is first
        job_lock.stop()

    def test_stop_can_decline_to_release(self, fake):
        job_lock = DetachedJobLock(HOLDER, interval=60)
        job_lock.start()
        job_lock.stop(release=False)

        assert fake.releases == []


def _wait(predicate, timeout=2.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False
