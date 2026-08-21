# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the CLI half of the detached-run lock handoff, in
cli/commands/development/python.py.

A detached run used to take an eternal lock (``ttl_seconds: null``) that only
`lager boxes unlock` could clear, because the CLI's heartbeat thread dies with
the CLI. The box now holds that lock for the job's lifetime instead. Two things
about the CLI's side of that are easy to get wrong and expensive if they are:

**Whose lock gets offered.** Only a lock this invocation freshly acquired. A
resumed `lager boxes lock` reservation carries the same holder string on a dev
machine (both fall back to the same user), so offering that one would let a
detached run quietly end somebody's deliberate hold on the bench.

**When the TTL is armed.** Not until the box confirms it is heartbeating. A CLI
that acquired with a TTL up front would, against a box too old to know about
the ``lock_holder`` field, let the lock lapse after 30 minutes while the
detached job was still driving the bench -- a regression manufactured purely by
version skew, and worse than the bug being fixed.
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


try:
    # `from cli.commands.development import python` yields the Click command,
    # not the module (the package exports the function under the same name).
    import cli.commands.development.python  # noqa: F401  # type: ignore[import]
    import sys as _sys
    cli_python = _sys.modules['cli.commands.development.python']
except Exception as _exc:  # pylint: disable=broad-except
    pytest.skip(
        f"Cannot import cli.commands.development.python "
        f"in this environment ({_exc}); requires the full CLI deps.",
        allow_module_level=True,
    )


HOLDER = 'benchuser@bench-1234'


@pytest.fixture(autouse=True)
def _reset_state():
    blank = {
        'active': False,
        'released': False,
        'box_ip': None,
        'holder': None,
        'box_label': None,
        'detach': False,
        'offer_to_box': False,
    }
    cli_python._AUTO_LOCK_STATE.update(blank)
    yield
    cli_python._AUTO_LOCK_STATE.update(blank)


def acquired_detached_lock():
    """The state after `lager python -d` freshly acquired the box lock."""
    cli_python._AUTO_LOCK_STATE.update({
        'active': True,
        'released': False,
        'box_ip': '10.0.0.9',
        'holder': HOLDER,
        'box_label': 'bench',
        'detach': True,
        'offer_to_box': True,
    })


def resumed_reservation():
    """The state after `lager python -d` found its own pre-existing lock.

    `state == 'already_ours'`: a `lager boxes lock` reservation, or a leftover
    ephemeral lock. Either way not ours to hand over -- so that branch never
    touches _AUTO_LOCK_STATE at all, and every flag stays at its default.
    """
    cli_python._AUTO_LOCK_STATE.update({
        'active': False,
        'released': False,
        'box_ip': None,
        'holder': None,
        'box_label': None,
        'detach': False,
        'offer_to_box': False,
    })


class TestWhoseLockGetsOffered:

    def test_a_freshly_acquired_lock_is_offered(self):
        acquired_detached_lock()
        assert cli_python._detach_lock_holder() == HOLDER

    def test_a_resumed_reservation_is_not_offered(self):
        """The guard that stops a detached run ending someone's reservation."""
        resumed_reservation()
        assert cli_python._detach_lock_holder() is None

    def test_no_lock_at_all_offers_nothing(self):
        assert cli_python._detach_lock_holder() is None


class TestArmingTheTtl:

    def test_an_unconfirmed_handoff_keeps_the_eternal_lock_and_says_so(
            self, monkeypatch, capsys):
        """An older box ignores lock_holder, so nothing would refresh a TTL.

        The CLI must notice that and behave exactly as it did before, banner
        and all, rather than arming a deadline the box will not honour.
        """
        acquired = []
        import cli.box_storage as box_storage
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **kw: (acquired.append(kw), ('acquired', {}))[1])

        acquired_detached_lock()
        cli_python._detach_lock_handoff({'status': 'detached'}, 'bench')

        assert acquired == [], 'must not arm a TTL the box will not refresh'
        err = capsys.readouterr().err
        assert 'lager boxes unlock --box bench' in err

    def test_a_confirmed_handoff_arms_the_ttl_backstop(self, monkeypatch, capsys):
        """The box releases at job end; the TTL covers the box service dying."""
        acquired = []
        import cli.box_storage as box_storage
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **kw: (acquired.append(kw), ('acquired', {}))[1])

        acquired_detached_lock()
        cli_python._detach_lock_handoff(
            {'status': 'detached', 'lock_held_by_box': True}, 'bench')

        assert len(acquired) == 1
        assert acquired[0]['ttl_seconds'] is not None
        assert acquired[0]['ttl_seconds'] > 0

        err = capsys.readouterr().err
        assert 'released when it ends' in err

    def test_a_missing_response_body_is_treated_as_unconfirmed(
            self, monkeypatch, capsys):
        acquired = []
        import cli.box_storage as box_storage
        monkeypatch.setattr(
            box_storage, 'acquire_box_lock',
            lambda *a, **kw: (acquired.append(kw), ('acquired', {}))[1])

        acquired_detached_lock()
        cli_python._detach_lock_handoff(None, 'bench')

        assert acquired == []
        assert 'lager boxes unlock' in capsys.readouterr().err

    def test_a_failure_to_arm_the_ttl_does_not_fail_the_launch(
            self, monkeypatch, capsys):
        """The job is running and the box is holding its lock either way."""
        import cli.box_storage as box_storage

        def boom(*a, **kw):
            raise RuntimeError('box unreachable')
        monkeypatch.setattr(box_storage, 'acquire_box_lock', boom)

        acquired_detached_lock()
        cli_python._detach_lock_handoff(
            {'lock_held_by_box': True}, 'bench')       # must not raise

        assert 'released when it ends' in capsys.readouterr().err

    def test_nothing_is_said_when_no_lock_was_taken(self, capsys):
        cli_python._detach_lock_handoff({'lock_held_by_box': True}, 'bench')
        assert capsys.readouterr().err == ''


class TestTheCliStillDoesNotReleaseOnExit:

    def test_detach_runs_skip_the_exit_release(self, monkeypatch):
        """The box owns the lock now, so the exiting CLI must keep its hands off.

        Unchanged behaviour, re-pinned here because the reason for it changed:
        it used to be "the detached job needs the lock and nobody else will
        hold it", and is now "somebody else is holding it".
        """
        released = []
        import cli.box_storage as box_storage
        monkeypatch.setattr(
            box_storage, 'release_box_lock',
            lambda *a, **kw: released.append(a))

        acquired_detached_lock()
        cli_python._auto_lock_release('test')

        assert released == []
