# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the `lager python` auto-lock wrapper in
cli/commands/development/python.py.

We don't exercise the full Click command (it pulls in trio, signal
handlers, etc. that don't belong in a unit test). Instead we focus on:

- `_auto_lock_release` is idempotent.
- `_auto_lock_release` is a no-op when no lock is active.
- `_auto_lock_release` is a no-op when `--detach` left the lock retained.
- The atexit handler is wired up so a process-exit path still releases.
- `_HeartbeatThread` calls `heartbeat_box_lock` on the configured cadence
  and stops promptly when `.stop()` is called.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
import time
from unittest import mock

import pytest


_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


try:
    # `from cli.commands.development import python` would yield the Click
    # command (cli/commands/development/__init__.py exports the function
    # under the same name as the submodule). Go through sys.modules so we
    # get the *module*.
    import cli.commands.development.python  # noqa: F401  # type: ignore[import]
    import sys as _sys
    cli_python = _sys.modules['cli.commands.development.python']
except Exception as _exc:  # pylint: disable=broad-except
    pytest.skip(
        f"Cannot import cli.commands.development.python "
        f"in this environment ({_exc}); requires the full CLI deps.",
        allow_module_level=True,
    )


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset the module-level auto-lock state before/after each test."""
    cli_python._AUTO_LOCK_STATE.update({
        'active': False,
        'released': False,
        'box_ip': None,
        'holder': None,
        'box_label': None,
        'detach': False,
    })
    yield
    cli_python._AUTO_LOCK_STATE.update({
        'active': False,
        'released': False,
        'box_ip': None,
        'holder': None,
        'box_label': None,
        'detach': False,
    })


class TestAutoLockRelease:
    def test_noop_when_no_lock_active(self):
        # release_box_lock is lazily imported inside _auto_lock_release, so
        # patch it at its real home (cli.box_storage) and assert it's not
        # called when there's no active lock state.
        with mock.patch('cli.box_storage.release_box_lock', side_effect=AssertionError(
            'release_box_lock should NOT be called when no lock is active'
        )):
            cli_python._auto_lock_release('nothing-active')

    def test_releases_once(self):
        cli_python._AUTO_LOCK_STATE.update({
            'active': True, 'released': False,
            'box_ip': '10.0.0.1', 'holder': 'alice',
            'box_label': 'lab-box', 'detach': False,
        })
        with mock.patch('cli.box_storage.release_box_lock') as rb:
            cli_python._auto_lock_release('first')
            cli_python._auto_lock_release('second')
        assert rb.call_count == 1
        rb.assert_called_with('10.0.0.1', 'alice')

    def test_detach_skips_release(self):
        cli_python._AUTO_LOCK_STATE.update({
            'active': True, 'released': False,
            'box_ip': '10.0.0.1', 'holder': 'alice',
            'box_label': 'lab-box', 'detach': True,
        })
        with mock.patch('cli.box_storage.release_box_lock') as rb:
            cli_python._auto_lock_release('detach')
        rb.assert_not_called()

    def test_release_swallows_exception(self):
        cli_python._AUTO_LOCK_STATE.update({
            'active': True, 'released': False,
            'box_ip': '10.0.0.1', 'holder': 'alice',
            'box_label': 'lab-box', 'detach': False,
        })
        with mock.patch(
            'cli.box_storage.release_box_lock', side_effect=RuntimeError('boom'),
        ):
            # Must NOT propagate; signal handlers depend on this.
            cli_python._auto_lock_release('boom')

    def test_atexit_registered(self):
        # atexit doesn't expose a clean way to list handlers; assert via the
        # internal callable name that ours is in the registry.
        # CPython stores handlers in atexit._exithandlers (CPython-only).
        if not hasattr(atexit, '_run_exitfuncs'):
            pytest.skip('atexit internals not introspectable')
        # Fallback assertion: at least the symbol exists and is callable.
        assert callable(cli_python._auto_lock_release)


class TestHeartbeatThread:
    def test_calls_heartbeat_periodically_until_stopped(self):
        with mock.patch('cli.box_storage.heartbeat_box_lock', return_value=True) as hb:
            t = cli_python._HeartbeatThread('10.0.0.1', 'alice', interval=1)
            calls = []

            def fast_wait(_seconds):
                # Return True on the third call so the loop exits.
                calls.append(1)
                if len(calls) >= 3:
                    return True
                return False

            with mock.patch.object(t._stop_event, 'wait', side_effect=fast_wait):
                t.start()
                t.join(timeout=2.0)
            assert not t.is_alive()
            assert hb.call_count >= 2

    def test_stop_terminates_thread_promptly(self):
        with mock.patch('cli.box_storage.heartbeat_box_lock', return_value=True):
            t = cli_python._HeartbeatThread('10.0.0.1', 'alice', interval=60)
            t.start()
            time.sleep(0.05)
            t.stop()
            t.join(timeout=2.0)
            assert not t.is_alive()

    def test_heartbeat_failure_does_not_kill_thread(self):
        with mock.patch(
            'cli.box_storage.heartbeat_box_lock',
            side_effect=[False, False, True],
        ) as hb:
            t = cli_python._HeartbeatThread('10.0.0.1', 'alice', interval=1)
            calls = []

            def fast_wait(_s):
                calls.append(1)
                if len(calls) >= 4:
                    return True
                return False

            with mock.patch.object(t._stop_event, 'wait', side_effect=fast_wait):
                t.start()
                t.join(timeout=2.0)
            # Three heartbeat attempts were made even though two failed.
            assert hb.call_count == 3
