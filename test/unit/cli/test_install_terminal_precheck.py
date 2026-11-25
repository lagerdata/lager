# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""`lager install --ip` asks up front whether it will be able to ask for sudo.

The deploy script's step 2 needs the box user's sudo password, and sudo reads
it from a terminal. Run from a pipe or a script, the install used to fail
there ("needs an interactive terminal"), minutes in. box_sudo_needs_a_terminal
is checked before the confirmation instead.
"""

import importlib
import subprocess
from unittest import mock

# The package re-exports the `install` command under the module's name.
install_mod = importlib.import_module("cli.commands.utility.install")


def _proc(code):
    return subprocess.CompletedProcess([], code, b"", b"")


def _check(code=None, raises=None, tty=False):
    calls = []

    def fake_run(argv, **_kw):
        calls.append(argv)
        if raises:
            raise raises
        return _proc(code)

    with mock.patch.object(install_mod.subprocess, "run", fake_run):
        answer = install_mod.box_sudo_needs_a_terminal(
            "benchtest@10.0.0.1", ["-i", "k"], isatty=lambda: tty)
    return answer, calls


def test_a_terminal_means_no_check_at_all():
    answer, calls = _check(code=1, tty=True)
    assert answer is False and calls == []


def test_passwordless_sudo_needs_no_terminal():
    answer, calls = _check(code=0)
    assert answer is False
    assert calls[0][-1] == "sudo -n true" and "BatchMode=yes" in calls[0]


def test_a_password_without_a_terminal_is_caught():
    answer, _ = _check(code=1)
    assert answer is True


def test_an_ssh_failure_is_not_taken_for_a_sudo_answer():
    assert _check(code=255)[0] is False
    assert _check(raises=subprocess.TimeoutExpired("ssh", 15))[0] is False
