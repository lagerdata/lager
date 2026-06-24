#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""CLI threading for the ``--settle`` option (GAP 2).

Covers the two pieces of real logic in ``cli/commands/communication/usb.py``:
  * ``_try_fast_path`` adds ``settle`` to the POST body (and stretches the
    client timeout so a long settle doesn't bounce to the slow path), and
    omits it entirely when unset.
  * ``_invoke_remote`` passes ``settle`` to the slow path as an optional 4th
    positional arg only when provided (keeping older 3-arg invocations valid).
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

usb = importlib.import_module("cli.commands.communication.usb")


def _fake_resp(status=200, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload or {"success": True, "message": "ok"}
    return resp


def test_fast_path_includes_settle_and_stretches_timeout():
    with patch("requests.post", return_value=_fake_resp()) as post:
        handled, _ = usb._try_fast_path("1.2.3.4", "usb1", "enable", settle=20.0)
    assert handled is True
    body = post.call_args.kwargs["json"]
    assert body["settle"] == 20.0
    # timeout must exceed the settle so the client waits for the box.
    assert post.call_args.kwargs["timeout"] >= 25.0


def test_fast_path_omits_settle_when_unset():
    with patch("requests.post", return_value=_fake_resp()) as post:
        usb._try_fast_path("1.2.3.4", "usb1", "enable")
    body = post.call_args.kwargs["json"]
    assert "settle" not in body
    assert post.call_args.kwargs["timeout"] == 10.0


def test_slow_path_appends_settle_arg():
    ctx = MagicMock()
    with patch.object(usb, "_try_fast_path", return_value=(False, None)), \
            patch.object(usb, "run_impl_script") as run_impl:
        usb._invoke_remote(ctx, "usb1", "1.2.3.4", "disable", settle=0.4)
    assert run_impl.call_args.kwargs["args"] == ("disable", "usb1", "0.4")


def test_slow_path_omits_settle_arg_when_unset():
    ctx = MagicMock()
    with patch.object(usb, "_try_fast_path", return_value=(False, None)), \
            patch.object(usb, "run_impl_script") as run_impl:
        usb._invoke_remote(ctx, "usb1", "1.2.3.4", "disable")
    assert run_impl.call_args.kwargs["args"] == ("disable", "usb1")
