#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Every impl script a command dispatches to must exist in the installed package.

`run_backend(ctx, box, "<script>.py", action, ...)` couples a command to a file
by a bare filename string. Nothing imports these scripts -- they are read off
disk and uploaded to the box -- so no import test, linter or type checker can
see a name that resolves to nothing.

Sixteen `lager logic` subcommands dispatched to `measurement.py`, `trigger.py`
and `cursor.py`, none of which are in the tree, and `get_impl_path` returned the
root-directory path without checking it existed. The commands failed with a bare
`ValueError` raised much later, after the box had been resolved and the net
validated, so it looked like a box problem.

This walks the call sites the way the CLI does and asserts each name resolves.
It fails on a new dispatch to a script nobody added, and on a script deleted out
from under a dispatch that still references it.
"""

import ast
import importlib
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, REPO_ROOT)

from cli.context.core import IMPL_SUBDIRS, get_impl_path  # noqa: E402
from cli.errors import LagerError  # noqa: E402

# Resolved as a module, not via a dotted mock.patch string: several
# cli/commands/**/__init__.py files re-export the click object under the same
# name as its module, which makes such strings resolve differently across
# Python versions.
logic_mod = importlib.import_module('cli.commands.measurement.logic')

CLI_DIR = os.path.join(REPO_ROOT, 'cli')
IMPL_DIR = os.path.join(CLI_DIR, 'impl')

# run_backend(ctx, dut, impl_script, action, **params) -- impl_script is 3rd.
_DISPATCHERS = {'run_backend': 2, 'run_impl_script': 2, 'get_impl_path': 0}

# Impl scripts that commands dispatch to and that are NOT in the tree.
#
# TWO-SIDED, like tools/packaging_import_baseline.txt: a name missing from this
# set fails the test, and a name here that starts resolving ALSO fails ("remove
# it"), so the set can only shrink honestly.
#
# All three are owned by #261. Sixteen `lager logic` subcommands dispatch to
# them -- `measure` (6 actions), `trigger` (5), `cursor` (5) -- with full option
# surfaces and complete docs in docs/source/reference/cli/logic.mdx, written
# against an implementation that either was removed or never landed. Restoring
# them is not a repoint at cli/impl/measurement/scope.py, which implements all
# sixteen action names: scope.py resolves nets with `role == "scope"` and
# NetType.Analog, and Net.get returns None on a type mismatch rather than
# raising, so a logic net would silently do nothing -- the defect #256 fixed for
# the five subcommands that do work. They need their own scripts on
# NetType.Logic, modelled on cli/impl/power/enable_disable.py.
# Empty, and it should stay that way. The three names that used to sit here
# were repointed at scope.py, which had always implemented their actions.
KNOWN_MISSING = frozenset()


def _resolves(script):
    try:
        get_impl_path(script)
        return True
    except LagerError:
        return False


def _dispatch_sites():
    """(script_name, relpath, lineno) for every literal impl-script argument."""
    sites = []
    for dirpath, dirnames, filenames in os.walk(CLI_DIR):
        dirnames[:] = [d for d in dirnames
                       if d not in ('vendor', 'elftools', '__pycache__')]
        for name in sorted(filenames):
            if not name.endswith('.py'):
                continue
            path = os.path.join(dirpath, name)
            try:
                tree = ast.parse(open(path, encoding='utf-8').read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                fname = func.attr if isinstance(func, ast.Attribute) else \
                    func.id if isinstance(func, ast.Name) else None
                if fname not in _DISPATCHERS:
                    continue
                idx = _DISPATCHERS[fname]
                if len(node.args) <= idx:
                    continue
                arg = node.args[idx]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    sites.append((arg.value,
                                  os.path.relpath(path, REPO_ROOT),
                                  node.lineno))
    return sites


class EveryDispatchedScriptExists(unittest.TestCase):

    def setUp(self):
        self.sites = _dispatch_sites()

    def test_the_scan_found_call_sites(self):
        """A scan that finds nothing would pass vacuously forever."""
        self.assertGreater(len(self.sites), 20,
                           'impl-script dispatch scan found almost nothing -- '
                           'the call shape probably changed and this test is '
                           'no longer looking at anything.')

    def test_every_dispatched_script_resolves(self):
        """Two-sided against KNOWN_MISSING, like tools/packaging_import_baseline.txt.

        A new unresolvable dispatch fails. So does one that starts resolving
        while still listed -- so the baseline can only shrink honestly.
        """
        missing = {script for script, _, _ in self.sites
                   if not _resolves(script)}

        new_breakage = sorted(missing - KNOWN_MISSING)
        self.assertEqual(
            new_breakage, [],
            'New dispatch to an impl script that is not in the tree:\n  '
            + '\n  '.join(f'{s!r} at ' + ', '.join(
                f'{r}:{n}' for sc, r, n in self.sites if sc == s)
                for s in new_breakage)
            + '\n\nAdd the script under cli/impl/, or remove the subcommand. '
              'Leaving it in --help while non-functional is the worst of the three.')

        fixed = sorted(KNOWN_MISSING - missing)
        self.assertEqual(
            fixed, [],
            f'{fixed} now resolve(s). Remove them from KNOWN_MISSING in this '
            'file to lock the fix in, and close out the matching part of #261.')


class GetImplPathResolution(unittest.TestCase):

    def test_subdirectory_script_resolves(self):
        self.assertEqual(
            os.path.dirname(get_impl_path('scope.py')),
            os.path.join(IMPL_DIR, 'measurement'))

    def test_root_fallback_still_resolves(self):
        """box_config.py lives at the root of impl/ and reaches it via the
        backward-compatibility fallback -- so the fallback cannot simply raise."""
        self.assertEqual(get_impl_path('box_config.py'),
                         os.path.join(IMPL_DIR, 'box_config.py'))

    def test_missing_script_raises_instead_of_returning_a_dead_path(self):
        with self.assertRaises(LagerError) as caught:
            get_impl_path('definitely_not_a_real_impl_script.py')
        self.assertIn('definitely_not_a_real_impl_script.py',
                      str(caught.exception.problem))

    def test_the_error_lists_where_it_looked(self):
        with self.assertRaises(LagerError) as caught:
            get_impl_path('definitely_not_a_real_impl_script.py')
        raw = caught.exception.raw or ''
        for subdir in IMPL_SUBDIRS:
            self.assertIn(os.path.join('impl', subdir), raw)

    def test_it_is_a_click_exception_not_a_traceback(self):
        """LagerError subclasses click.ClickException, so click renders it and
        exits rather than dumping a traceback at the user."""
        import click
        with self.assertRaises(click.ClickException):
            get_impl_path('definitely_not_a_real_impl_script.py')


class TheUserSeesAMessageNotATraceback(unittest.TestCase):
    """What `lager logic <net> measure period` actually prints.

    The box and the net are mocked because the failure is entirely CLI-side --
    and because it arrives *after* both have been checked over the network,
    which is what made it read as a box fault.
    """

    class _Obj:
        """Settable stand-in for the LagerContext -- the group stashes
        `netname` on it. Same shape as test_watt_subcommands.py's."""

    def _invoke(self, args):
        from click.testing import CliRunner
        with mock.patch.object(logic_mod, '_resolve_box',
                               return_value='192.0.2.10'), \
             mock.patch.object(logic_mod, '_require_netname',
                               return_value='logic1'), \
             mock.patch.object(logic_mod, '_validate_logic_net',
                               return_value=True):
            return CliRunner().invoke(logic_mod.logic, args,
                                      obj=self._Obj(),
                                      catch_exceptions=False)

    def test_a_genuinely_missing_script_names_itself_without_a_traceback(self):
        """The guardrail's user-facing contract, through a real command.

        Driven with a script name that is deliberately absent rather than with
        `measure period`, which used to be absent and is not any more (#261).
        A test that needs a real bug in the tree to pass stops testing anything
        the moment the bug is fixed.
        """
        def _dispatch_missing(ctx, dut, action, **params):
            return logic_mod.run_backend(ctx, dut, 'definitely_not_here.py',
                                         action, **params)

        with mock.patch.object(logic_mod, '_run_measurement_backend',
                               _dispatch_missing):
            result = self._invoke(['logic1', 'measure', 'period'])
        self.assertNotIn('Traceback (most recent call last)', result.output)
        self.assertNotIn('Could not find runnable', result.output)
        self.assertIn('definitely_not_here.py', result.output)
        self.assertEqual(result.exit_code, 1)

    def test_the_formerly_dead_commands_now_reach_the_backend(self):
        """#261's sixteen actions dispatch instead of erroring.

        Asserts the script *and* the action, because both were wrong:
        `measurement.py`/`trigger.py`/`cursor.py` had been consolidated into
        `scope.py`, and the two pulse-width actions were additionally sent
        under names `scope.py` does not register.
        """
        cases = [
            (['logic1', 'measure', 'period'], 'scope.py', 'measure_period'),
            (['logic1', 'measure', 'pw-pos'], 'scope.py', 'measure_pulse_width_pos'),
            (['logic1', 'measure', 'pw-neg'], 'scope.py', 'measure_pulse_width_neg'),
            (['logic1', 'trigger', 'edge'], 'scope.py', 'trigger_edge'),
            (['logic1', 'cursor', 'hide'], 'scope.py', 'hide_cursor'),
        ]
        for args, script, action in cases:
            with self.subTest(command=' '.join(args)):
                with mock.patch.object(logic_mod, 'run_backend') as backend:
                    result = self._invoke(args)
                self.assertNotIn('has no implementation in this release',
                                 result.output)
                backend.assert_called_once()
                self.assertEqual(backend.call_args.args[2], script)
                self.assertEqual(backend.call_args.args[3], action)


if __name__ == '__main__':
    unittest.main()
