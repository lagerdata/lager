# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager ssh` holds an ephemeral auto-lock for the whole session — interactive
shell and one-shot remote command alike — so other lager users can see that
someone is on the box. Direct `ssh user@box` still bypasses it.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
from unittest import mock

import pytest
from click.testing import CliRunner


_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# `from cli.commands.box import ssh` yields the Click command (re-exported by
# box/__init__.py). Load the module so we can patch its helpers.
ssh_mod = importlib.import_module("cli.commands.box.ssh")
box_storage = importlib.import_module("cli.box_storage")


@pytest.fixture(autouse=True)
def _clear_auto_lock_disable(monkeypatch):
    """These tests assert the lock path; clear the escape hatch if a parent set it."""
    monkeypatch.delenv("LAGER_AUTO_LOCK_DISABLE", raising=False)


def _invoke(args, *, ssh_rc=0):
    """Run `lager ssh` with resolve/SSH/lock seams faked."""
    lock_calls = []
    resolve_calls = []

    @contextlib.contextmanager
    def fake_lock(ip, label, command_name, **kw):
        # ssh must fail open, or a box without the lock API gets no shell.
        assert kw.get("fail_open") is True
        lock_calls.append(("enter", ip, label, command_name))
        try:
            yield ("holder", "acquired")
        finally:
            lock_calls.append(("exit", ip, label, command_name))

    def tracking_resolve(_ctx, _box, **kw):
        resolve_calls.append(kw)
        return "10.0.0.1"

    with mock.patch.object(ssh_mod, "resolve_and_validate_box", tracking_resolve), \
            mock.patch.object(ssh_mod, "auto_lock_around_command", fake_lock), \
            mock.patch.object(ssh_mod.subprocess, "call", return_value=ssh_rc), \
            mock.patch.object(ssh_mod, "_identity_args", return_value=[]), \
            mock.patch("cli.box_storage.get_box_user", return_value=None), \
            mock.patch.object(ssh_mod, "resolve_box_user", return_value="lagerdata"):
        result = CliRunner().invoke(ssh_mod.ssh, list(args), catch_exceptions=False)

    return result, lock_calls, resolve_calls


class TestSshAutoLock:
    def test_interactive_acquires_and_releases(self):
        result, lock_calls, resolve_calls = _invoke(["--box", "lab-box"])
        assert result.exit_code == 0, result.output
        assert lock_calls == [
            ("enter", "10.0.0.1", "lab-box", "ssh"),
            ("exit", "10.0.0.1", "lab-box", "ssh"),
        ]
        assert resolve_calls == [{"_skip_lock_check": True}]

    def test_one_shot_acquires_and_releases(self):
        result, lock_calls, _ = _invoke(
            ["--box", "lab-box", "--", "ls", "-la", "/etc/lager"],
        )
        assert result.exit_code == 0, result.output
        assert lock_calls == [
            ("enter", "10.0.0.1", "lab-box", "ssh"),
            ("exit", "10.0.0.1", "lab-box", "ssh"),
        ]

    def test_lock_releases_when_ssh_fails(self):
        result, lock_calls, _ = _invoke(["--box", "lab-box"], ssh_rc=255)
        assert result.exit_code == 255
        assert [c[0] for c in lock_calls] == ["enter", "exit"]

    def test_disable_env_skips_acquire_keeps_precheck(self, monkeypatch):
        monkeypatch.setenv("LAGER_AUTO_LOCK_DISABLE", "1")
        resolve_calls = []

        def tracking_resolve(_ctx, _box, **kw):
            resolve_calls.append(kw)
            return "10.0.0.1"

        with mock.patch.object(ssh_mod, "resolve_and_validate_box", tracking_resolve), \
                mock.patch.object(ssh_mod, "auto_lock_around_command",
                                  box_storage.auto_lock_around_command), \
                mock.patch.object(box_storage, "acquire_box_lock",
                                  side_effect=AssertionError("acquire must not run")), \
                mock.patch.object(ssh_mod.subprocess, "call", return_value=0), \
                mock.patch.object(ssh_mod, "_identity_args", return_value=[]), \
                mock.patch("cli.box_storage.get_box_user", return_value=None), \
                mock.patch.object(ssh_mod, "resolve_box_user", return_value="lagerdata"):
            result = CliRunner().invoke(
                ssh_mod.ssh, ["--box", "lab-box"], catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert resolve_calls == [{"_skip_lock_check": False}]


class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.mark.parametrize("status", [404, 500])
def test_box_without_lock_api_still_gets_a_shell(monkeypatch, status):
    # The real lock code against a box whose image has no lock API (404) or
    # whose lock server errors: ssh warns and connects instead of exiting.
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(status))
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(status))
    calls = []
    with mock.patch.object(ssh_mod, "resolve_and_validate_box",
                           lambda _ctx, _box, **_kw: "10.0.0.1"), \
            mock.patch.object(ssh_mod, "auto_lock_around_command",
                              box_storage.auto_lock_around_command), \
            mock.patch.object(box_storage, "_gateway_kwargs", lambda _ip: {}), \
            mock.patch.object(box_storage, "_check_gateway", lambda resp, _ip: resp), \
            mock.patch.object(ssh_mod.subprocess, "call",
                              lambda cmd, **_k: calls.append(cmd) or 0), \
            mock.patch.object(ssh_mod, "_identity_args", return_value=[]), \
            mock.patch("cli.box_storage.get_box_user", return_value=None), \
            mock.patch.object(ssh_mod, "resolve_box_user", return_value="lagerdata"):
        result = CliRunner().invoke(ssh_mod.ssh, ["--box", "lab-box"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "continuing without a lock" in result.output
