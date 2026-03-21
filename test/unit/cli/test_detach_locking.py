# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Tests that lager python --detach holds the command lock (busy.json)
so other commands see the box as busy.
"""

import os
import sys
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from click.testing import CliRunner
import click


class FakeLagerContext:
    """Minimal stand-in for cli.context.core.LagerContext."""

    def __init__(self):
        self.defaults = {}
        self.style = lambda string, **kw: string
        self.debug = False
        self.interpreter = None
        self.force_command = False

    def get_session_for_box(self, box, box_name=None):
        return MagicMock()


def _invoke_python_command(args, run_internal_side_effect=None):
    """Invoke the python subcommand with a FakeLagerContext on ctx.obj."""
    from cli.commands.development.python import python

    side_effect = run_internal_side_effect or (lambda *a, **kw: None)

    @click.group()
    @click.pass_context
    def fake_cli(ctx):
        ctx.obj = FakeLagerContext()

    fake_cli.add_command(python)

    runner = CliRunner()
    with runner.isolated_filesystem():
        with open('test_script.py', 'w') as f:
            f.write('print("hello")\n')

        with patch(
            'cli.commands.development.python.run_python_internal',
            side_effect=side_effect,
        ):
            result = runner.invoke(fake_cli, ['python'] + args)

    return result


class TestDetachLocking:
    """Verify that --detach sets _skip_lock_release before run_python_internal."""

    @patch('cli.box_storage._release_command_lock')
    @patch('cli.box_storage._acquire_command_lock')
    @patch('cli.box_storage.get_box_ip', return_value='10.0.0.1')
    @patch('cli.box_storage.get_lager_user', return_value='testuser')
    def test_skip_lock_release_set_before_run(
        self, mock_user, mock_get_ip, mock_acquire, mock_release,
    ):
        """
        _skip_lock_release must be True before run_python_internal is
        called, so the cleanup callback never releases the command lock.
        """
        observed_flag = []

        def spy_run_internal(ctx, *args, **kwargs):
            flag = getattr(getattr(ctx, 'obj', None), '_skip_lock_release', False)
            observed_flag.append(flag)

        result = _invoke_python_command(
            ['test_script.py', '--box', 'TESTBOX', '--detach'],
            run_internal_side_effect=spy_run_internal,
        )

        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert len(observed_flag) == 1, (
            f"run_python_internal should have been called once, got {len(observed_flag)}"
        )
        assert observed_flag[0] is True, (
            "_skip_lock_release must be True BEFORE run_python_internal is called"
        )

    @patch('cli.box_storage._release_command_lock')
    @patch('cli.box_storage._acquire_command_lock')
    @patch('cli.box_storage.get_box_ip', return_value='10.0.0.1')
    @patch('cli.box_storage.get_lager_user', return_value='testuser')
    def test_cleanup_does_not_release_lock_on_detach(
        self, mock_user, mock_get_ip, mock_acquire, mock_release,
    ):
        """
        After a successful --detach, the cleanup callback must NOT call
        _release_command_lock.
        """
        result = _invoke_python_command(
            ['test_script.py', '--box', 'TESTBOX', '--detach'],
        )

        assert result.exit_code == 0, f"Command failed: {result.output}"
        mock_release.assert_not_called()

    @patch('cli.box_storage._release_command_lock')
    @patch('cli.box_storage._acquire_command_lock')
    @patch('cli.box_storage.get_box_ip', return_value='10.0.0.1')
    @patch('cli.box_storage.get_lager_user', return_value='testuser')
    def test_non_detach_does_release_lock(
        self, mock_user, mock_get_ip, mock_acquire, mock_release,
    ):
        """
        Without --detach, the cleanup callback SHOULD release the lock.
        """
        result = _invoke_python_command(
            ['test_script.py', '--box', 'TESTBOX'],
        )

        assert result.exit_code == 0, f"Command failed: {result.output}"
        mock_release.assert_called_once()

    @patch('cli.box_storage._release_command_lock')
    @patch('cli.box_storage._acquire_command_lock')
    @patch('cli.box_storage.get_box_ip', return_value='10.0.0.1')
    @patch('cli.box_storage.get_lager_user', return_value='testuser')
    def test_skip_lock_not_set_for_non_detach(
        self, mock_user, mock_get_ip, mock_acquire, mock_release,
    ):
        """
        Without --detach, _skip_lock_release should remain unset/False.
        """
        observed_flag = []

        def spy_run_internal(ctx, *args, **kwargs):
            flag = getattr(getattr(ctx, 'obj', None), '_skip_lock_release', False)
            observed_flag.append(flag)

        result = _invoke_python_command(
            ['test_script.py', '--box', 'TESTBOX'],
            run_internal_side_effect=spy_run_internal,
        )

        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert len(observed_flag) == 1
        assert observed_flag[0] is False, (
            "_skip_lock_release should be False for non-detach mode"
        )

    @patch('cli.box_storage._release_command_lock')
    @patch('cli.box_storage._acquire_command_lock')
    @patch('cli.box_storage.get_box_ip', return_value='10.0.0.1')
    @patch('cli.box_storage.get_lager_user', return_value='testuser')
    def test_lock_acquired_for_detach(
        self, mock_user, mock_get_ip, mock_acquire, mock_release,
    ):
        """
        --detach must still acquire the command lock (just not release it).
        """
        result = _invoke_python_command(
            ['test_script.py', '--box', 'TESTBOX', '--detach'],
        )

        assert result.exit_code == 0, f"Command failed: {result.output}"
        mock_acquire.assert_called_once()
