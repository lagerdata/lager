# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Stop-signal handling for `lager python`.

Handlers used to be installed for SIGINT only, so `kill -TERM` -- and the
second rung of a GitHub Actions cancellation, and a dropped SSH session --
killed the client at its default disposition. That skipped both auto-lock
release paths and never sent the kill RPC, so the cloud box lock leaked and
the box-side script was left running until the disconnect reaper noticed it on
its next write, with the bench still live.

These tests assert on the process's real signal dispositions rather than on a
recorded call, because the defect was precisely that a disposition was left at
its default.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from unittest import mock

import pytest


_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


try:
    # `from cli.commands.development import python` would yield the Click
    # command, not the module. Go through sys.modules.
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
def _restore_dispositions():
    """Put every stop signal back however this test found it.

    Installing handlers is process-global, so a test that leaves one behind
    would change how the rest of the suite responds to a Ctrl+C.
    """
    saved = {sig: signal.getsignal(sig) for sig in cli_python._STOP_SIGNALS}
    saved_originals = dict(cli_python._ORIGINAL_STOP_HANDLERS)
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)
    cli_python._ORIGINAL_STOP_HANDLERS.clear()
    cli_python._ORIGINAL_STOP_HANDLERS.update(saved_originals)


def test_stop_signals_cover_term_and_hup():
    """SIGTERM is what a supervisor sends; SIGHUP is a dropped SSH session."""
    assert signal.SIGINT in cli_python._STOP_SIGNALS
    assert signal.SIGTERM in cli_python._STOP_SIGNALS
    assert signal.SIGHUP in cli_python._STOP_SIGNALS


def test_installs_a_handler_for_every_stop_signal():
    kill_python = mock.Mock()

    cli_python._install_stop_handlers(kill_python, '10.0.0.1')

    for sig in cli_python._STOP_SIGNALS:
        handler = signal.getsignal(sig)
        assert handler not in (signal.SIG_DFL, signal.SIG_IGN), sig
        assert handler.func is cli_python.sigint_handler, sig


@pytest.mark.parametrize(
    'sig', [signal.SIGINT, signal.SIGTERM, signal.SIGHUP], ids=str
)
def test_each_stop_signal_sends_the_kill_rpc(sig):
    """The regression: only SIGINT used to reach the box at all."""
    kill_python = mock.Mock()
    cli_python._install_stop_handlers(kill_python, '10.0.0.1')

    signal.getsignal(sig)(sig, None)

    kill_python.assert_called_once_with(signal.SIGTERM)


def test_handler_restores_every_disposition_before_the_rpc():
    """A second stop signal must not be swallowed while the RPC is in flight.

    The escape hatch is deliberate: it lets an operator break out of a kill
    that is hanging against an unreachable box. It only works if the restore
    happens before the (blocking) RPC, so the assertion is made from inside
    the mocked call rather than after it.
    """
    observed = {}

    def record(_sig):
        for sig in cli_python._STOP_SIGNALS:
            observed[sig] = signal.getsignal(sig)

    cli_python._install_stop_handlers(mock.Mock(side_effect=record), '10.0.0.1')
    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    assert observed
    for sig, handler in observed.items():
        assert handler is cli_python._ORIGINAL_STOP_HANDLERS[sig], sig


def test_restore_puts_back_the_pre_run_dispositions():
    cli_python._ORIGINAL_STOP_HANDLERS.clear()
    for sig in cli_python._STOP_SIGNALS:
        signal.signal(sig, signal.SIG_DFL)

    cli_python._install_stop_handlers(mock.Mock(), '10.0.0.1')
    cli_python._restore_stop_handlers()

    for sig in cli_python._STOP_SIGNALS:
        assert signal.getsignal(sig) == signal.SIG_DFL, sig


def test_a_failed_rpc_is_reported_not_raised():
    """Raising out of a signal handler would unwind the streaming loop from
    whatever line the signal happened to interrupt."""
    import requests

    kill_python = mock.Mock(side_effect=requests.exceptions.ConnectTimeout('nope'))
    cli_python._install_stop_handlers(kill_python, '10.0.0.1')

    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    kill_python.assert_called_once()


def test_install_is_a_no_op_off_the_main_thread():
    """signal.signal() raises ValueError off the main thread, and the Net TUI
    runs box calls on worker threads."""
    for sig in cli_python._STOP_SIGNALS:
        signal.signal(sig, signal.SIG_DFL)

    error = []

    def worker():
        try:
            cli_python._install_stop_handlers(mock.Mock(), '10.0.0.1')
            cli_python._restore_stop_handlers()
        except BaseException as exc:  # pylint: disable=broad-except
            error.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert not error
    for sig in cli_python._STOP_SIGNALS:
        assert signal.getsignal(sig) == signal.SIG_DFL, sig
