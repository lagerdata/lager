# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the breakpoint client helpers in cli/context/session.py.

Pins the request shape of DirectHTTPSession.continue_python /
breakpoint_status so the CLI and box endpoints stay in agreement.
"""

from __future__ import annotations

import os
import sys
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
sys.path.insert(0, _REPO_ROOT)

from cli.context.session import DirectHTTPSession  # noqa: E402


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
