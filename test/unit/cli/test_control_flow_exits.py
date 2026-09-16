#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`ctx.exit(N)` must survive the broad handler of the try block it was raised in.

`click.exceptions.Exit` subclasses RuntimeError, so `except Exception` catches
it. Where that happened, the command printed a traceback for a designed exit
and then replaced the code with 1 -- so `lager update --check` reported "an
update is available" (1) for a box it had never reached (2), and the
integration workflow had to parse stdout because the exit code could not tell
those apart.

Two layers here:
  1. The behaviour, end to end through CliRunner, on the call site that was
     reported.
  2. The shape, across cli/commands/, via tools/check_control_flow_handlers.py
     -- and a check that the checker itself still detects the bad shapes,
     because a static check that cannot fail is worse than none.
"""

import importlib
import os
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

from click.testing import CliRunner

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, REPO_ROOT)

# The MODULE, resolved explicitly. `cli/commands/utility/__init__.py` re-exports
# the click Command as `update`, so the dotted string
# 'cli.commands.utility.update' is ambiguous: mock.patch resolves it to the
# module on 3.11+ and to the Command on 3.10, where the patches then fail with
# "<Command update> does not have the attribute ...". patch.object against this
# does no string resolution at all.
update_mod = importlib.import_module('cli.commands.utility.update')
update = update_mod.update

CHECKER = os.path.join(REPO_ROOT, 'tools', 'check_control_flow_handlers.py')


class CheckExitCodeSurvives(unittest.TestCase):
    """`lager update --check` against a box with no usable lager_box key.

    update.py's own code asks for 2 there. Before the fix the generic handler
    turned that into a traceback plus rc 1.
    """

    def _run(self, extra=(), managed=False):
        runner = CliRunner()
        # box_has_control_plane MUST be mocked: it runs its own subprocess.run
        # against the destination, so leaving it live makes every test here
        # open a real SSH connection to 192.0.2.10 and wait out the connect
        # timeout. Default False keeps these cases on the path they were
        # written for -- an unreachable box is not a managed one.
        with mock.patch.object(update_mod, 'resolve_and_validate_box',
                               return_value='192.0.2.10'), \
             mock.patch.object(update_mod, 'get_box_user',
                               return_value='lagerdata'), \
             mock.patch.object(update_mod, 'box_has_control_plane',
                               return_value=managed), \
             mock.patch.object(update_mod, 'box_accepts_a_password',
                               return_value=None), \
             mock.patch.object(update_mod, 'key_installed_on_box',
                               return_value=False):
            return runner.invoke(
                update, ['--box', 'testbox', '--check', *extra],
                catch_exceptions=False)

    def test_exit_code_is_2_not_1(self):
        result = self._run()
        self.assertEqual(
            result.exit_code, 2,
            "--check must exit 2 when it could not reach the box. rc 1 means "
            "'an update is available', which it is in no position to claim.\n"
            f"output:\n{result.output}")

    def test_no_traceback_for_a_designed_exit(self):
        result = self._run()
        self.assertNotIn('Traceback (most recent call last)', result.output)
        self.assertNotIn('click.exceptions.Exit', result.output)

    def test_exit_payload_is_not_printed_as_an_error_message(self):
        """`log_error(f'Error: {str(e)}')` on an Exit rendered as `Error: 2`."""
        result = self._run()
        self.assertNotIn('Error: 2', result.output)

    def test_the_designed_message_still_reaches_the_user(self):
        result = self._run()
        self.assertIn('SSH key not configured for this box', result.output)

    def test_verbose_does_not_change_the_code(self):
        self.assertEqual(self._run(extra=['--verbose']).exit_code, 2)


class AManagedBoxNeedsNoLagerKey(unittest.TestCase):
    """A box whose SSH keys a control plane manages, reached on one of the
    operator's own identities.

    key_installed_on_box returns False -- lager_box is absent -- but False
    also means the box ANSWERED, so something authenticated. `lager update`
    used to read that as "SSH key not configured" and offer to install its
    own key, because it asked whether lager_box specifically worked rather
    than whether anything did. Every accepted offer appended another
    lager-box-access line outside every manager's block, on a box that
    already had working access."""

    def _run(self):
        runner = CliRunner()
        with mock.patch.object(update_mod, 'resolve_and_validate_box',
                               return_value='192.0.2.10'), \
             mock.patch.object(update_mod, 'get_box_user',
                               return_value='lagerdata'), \
             mock.patch.object(update_mod, 'box_has_control_plane',
                               return_value=True), \
             mock.patch.object(update_mod, 'box_accepts_a_password',
                               return_value=None), \
             mock.patch.object(update_mod, 'key_installed_on_box',
                               return_value=False), \
             mock.patch.object(update_mod, 'subprocess') as sub:
            # Downstream steps would SSH for real; a mock keeps them from
            # reaching the network. What they return does not matter here --
            # the assertions are all about the auth decision above them.
            sub.run.return_value = mock.Mock(returncode=1, stdout='', stderr='')
            return runner.invoke(
                update, ['--box', 'testbox', '--check'],
                catch_exceptions=False)

    def test_does_not_report_the_key_as_unconfigured(self):
        self.assertNotIn('SSH key not configured for this box',
                         self._run().output)

    def test_does_not_offer_to_install_a_key(self):
        self.assertNotIn('Set up SSH key for this box?', self._run().output)

    def test_says_which_key_it_is_using(self):
        self.assertIn('using your control-plane key', self._run().output)


class NoBroadHandlerSwallowsControlFlow(unittest.TestCase):
    """The shape, not the one call site -- see the checker's module docstring."""

    def test_cli_commands_is_clean(self):
        proc = subprocess.run(
            [sys.executable, CHECKER, 'cli/commands'],
            cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(
            proc.returncode, 0,
            f'tools/check_control_flow_handlers.py reports:\n'
            f'{proc.stdout}{proc.stderr}')


class TheCheckerDetectsTheBadShapes(unittest.TestCase):
    """A gate that cannot fail is not a gate."""

    CASES = {
        'swallowed': ("""
            def f(ctx):
                try:
                    ctx.exit(2)
                except Exception:
                    ctx.exit(1)
            """, True),
        'wrong_order': ("""
            from click.exceptions import Abort, Exit
            def f(ctx):
                try:
                    ctx.exit(2)
                except Exception:
                    ctx.exit(1)
                except (Abort, Exit):
                    raise
            """, True),
        'bare_except': ("""
            def f(ctx):
                try:
                    ctx.exit(2)
                except:
                    ctx.exit(1)
            """, True),
        'raise_abort': ("""
            import click
            def f(ctx):
                try:
                    raise click.Abort()
                except Exception:
                    ctx.exit(1)
            """, True),
        'reraised': ("""
            from click.exceptions import Abort, Exit
            def f(ctx):
                try:
                    ctx.exit(2)
                except (Exit, Abort):
                    raise
                except Exception:
                    ctx.exit(1)
            """, False),
        'no_control_flow_in_body': ("""
            def f(ctx):
                try:
                    risky()
                except Exception:
                    ctx.exit(1)
            """, False),
        'narrow_handler_only': ("""
            def f(ctx):
                try:
                    ctx.exit(2)
                except OSError:
                    ctx.exit(1)
            """, False),
    }

    def test_each_case(self):
        runner = CliRunner()
        for name, (src, should_fail) in self.CASES.items():
            with self.subTest(case=name):
                with runner.isolated_filesystem() as tmp:
                    with open(os.path.join(tmp, 'probe.py'), 'w') as fh:
                        fh.write(textwrap.dedent(src))
                    proc = subprocess.run(
                        [sys.executable, CHECKER, tmp],
                        capture_output=True, text=True)
                    if should_fail:
                        self.assertEqual(proc.returncode, 1,
                                         f'{name} should be reported')
                    else:
                        self.assertEqual(proc.returncode, 0,
                                         f'{name} is a false positive:\n'
                                         f'{proc.stdout}{proc.stderr}')


if __name__ == '__main__':
    unittest.main()
