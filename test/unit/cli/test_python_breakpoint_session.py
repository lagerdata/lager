# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the breakpoint client helpers in cli/context/session.py.

Pins the request shape of DirectHTTPSession.continue_python /
breakpoint_status so the CLI and box endpoints stay in agreement.
"""

from __future__ import annotations

import io
import os
import sys
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
sys.path.insert(0, _REPO_ROOT)

from cli.context.session import DirectHTTPSession  # noqa: E402
from cli.commands.development.python import (  # noqa: E402
    _should_watch_stdin_for_resume,
)


class _Tty:
    """Minimal stdin/stdout stand-in with a controllable isatty()."""

    def __init__(self, isatty):
        self._isatty = isatty

    def isatty(self):
        return self._isatty


def test_watch_stdin_gate_allows_genuine_foreground_run():
    """Interactive, streaming run with the real stdout: watcher runs."""
    with mock.patch.object(sys, "stdin", _Tty(True)), mock.patch.object(
        sys, "stdout", sys.__stdout__
    ):
        assert _should_watch_stdin_for_resume(True, None) is True


def test_watch_stdin_gate_suppressed_when_stdout_redirected():
    """Capture call sites swap sys.stdout (redirect_stdout): watcher suppressed.

    This is the regression guard for the v0.21.0 lager.pause() breakpoint: net
    validation / TUIs / webcam / debug all capture stdout into a buffer, and the
    watcher must not leak a stdin reader that races a later TUI or confirm.
    """
    buf = io.StringIO()
    with mock.patch.object(sys, "stdin", _Tty(True)), mock.patch.object(
        sys, "stdout", buf
    ):
        assert sys.stdout is not sys.__stdout__
        assert _should_watch_stdin_for_resume(True, None) is False


def test_watch_stdin_gate_respects_explicit_opt_out():
    """watch_stdin_resume=False (the Textual TUIs) always suppresses it."""
    with mock.patch.object(sys, "stdin", _Tty(True)), mock.patch.object(
        sys, "stdout", sys.__stdout__
    ):
        assert _should_watch_stdin_for_resume(False, None) is False


def test_watch_stdin_gate_suppressed_without_tty():
    """No human at the keyboard (non-tty stdin): watcher suppressed."""
    with mock.patch.object(sys, "stdin", _Tty(False)), mock.patch.object(
        sys, "stdout", sys.__stdout__
    ):
        assert _should_watch_stdin_for_resume(True, None) is False


def test_watch_stdin_gate_suppressed_for_capture_callback():
    """A non-None callback marks an output-capture run: watcher suppressed."""
    with mock.patch.object(sys, "stdin", _Tty(True)), mock.patch.object(
        sys, "stdout", sys.__stdout__
    ):
        assert _should_watch_stdin_for_resume(True, lambda *a: None) is False


def _session():
    s = DirectHTTPSession.__new__(DirectHTTPSession)
    s.box_ip = "1.2.3.4"
    s.base_url = "http://1.2.3.4:5000"
    s.session = mock.Mock()
    return s


def test_continue_python_request_shape():
    s = _session()
    s.session.post.return_value = "resp"
    out = s.continue_python("ignored", "PID-123")
    assert out == "resp"
    args, kwargs = s.session.post.call_args
    assert args[0] == "http://1.2.3.4:5000/python/continue"
    assert kwargs["json"] == {"lager_process_id": "PID-123"}


def test_breakpoint_status_request_shape():
    s = _session()
    s.session.post.return_value = "resp"
    out = s.breakpoint_status("ignored", "PID-123")
    assert out == "resp"
    args, kwargs = s.session.post.call_args
    assert args[0] == "http://1.2.3.4:5000/python/breakpoint"
    assert kwargs["json"] == {"lager_process_id": "PID-123"}
