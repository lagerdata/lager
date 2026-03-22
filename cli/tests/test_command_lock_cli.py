# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for CLI command-lock helpers (force flag and argv handling)."""

from unittest import mock

from cli.box_storage import acquire_command_lock_with_cleanup
from cli.context.core import argv_declares_force_command


def test_argv_declares_force_command_after_subcommand():
    assert argv_declares_force_command(["ssh", "--box", "JUL-4", "--force-command"])


def test_argv_declares_force_command_ignores_after_double_dash():
    assert not argv_declares_force_command(["python", "s.py", "--", "--force-command"])


def test_acquire_command_lock_with_cleanup_merges_ctx_obj_force():
    captured = {}

    def _fake_acquire(ip, box_name, command_name, force=False):
        captured["force"] = force

    ctx = mock.Mock()
    ctx.obj = mock.Mock(force_command=True)
    ctx.call_on_close = mock.Mock()

    with mock.patch("cli.box_storage._acquire_command_lock", _fake_acquire):
        acquire_command_lock_with_cleanup(ctx, "10.0.0.1", "lab", "install", force=False)

    assert captured["force"] is True
