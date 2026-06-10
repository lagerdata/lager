# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit test for box/lager/debug/gdb.py — get_controller() must close the
GdbController it builds on a failed/retried attempt.

A controller from a failed attempt is never stored in _gdb_controller_cache,
so cleanup_controller() can never reach it. Without an explicit close, its
gdb-multiarch subprocess and the pipe fds to it leak; enough leaks push the
long-lived debug service past 1024 open fds and break select() in pexpect
(erase/flash) and RTT streaming.

pygdbmi is not a test dependency, so a minimal stub module is injected into
sys.modules before gdb.py is loaded via importlib (matching the importlib
pattern used by the other box unit tests).
"""

import importlib.util
import os
import sys
import types
import unittest
from unittest import mock


HERE = os.path.dirname(__file__)
GDB_PATH = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'debug', 'gdb.py')
)


class _StubGdbTimeoutError(Exception):
    """Stand-in for pygdbmi.constants.GdbTimeoutError."""


def _install_pygdbmi_stub():
    """Inject a minimal fake pygdbmi so gdb.py imports without the real dep."""
    pygdbmi = types.ModuleType('pygdbmi')
    gdbcontroller = types.ModuleType('pygdbmi.gdbcontroller')
    constants = types.ModuleType('pygdbmi.constants')
    gdbcontroller.GdbController = object  # overridden per-test via mock.patch
    constants.GdbTimeoutError = _StubGdbTimeoutError
    pygdbmi.gdbcontroller = gdbcontroller
    pygdbmi.constants = constants
    sys.modules.setdefault('pygdbmi', pygdbmi)
    sys.modules.setdefault('pygdbmi.gdbcontroller', gdbcontroller)
    sys.modules.setdefault('pygdbmi.constants', constants)


def _load_gdb():
    _install_pygdbmi_stub()
    spec = importlib.util.spec_from_file_location('lager_debug_gdb', GDB_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gdb = _load_gdb()


class GetControllerLeakTests(unittest.TestCase):
    def setUp(self):
        gdb._gdb_controller_cache.clear()
        gdb._gdb_use_counts.clear()

    def test_failed_attempts_close_their_controllers(self):
        """Every GdbController built by a failed get_controller() retry must be
        .exit()'d so its gdb-multiarch subprocess / pipe fds don't leak."""
        created = []

        def fake_ctor(*_args, **_kwargs):
            ctrl = mock.MagicMock(name='GdbController')
            # Fail setup so the attempt never reaches the cache.
            ctrl.get_gdb_response.side_effect = gdb.GdbTimeoutError('timeout')
            created.append(ctrl)
            return ctrl

        with mock.patch.object(gdb, 'GdbController', side_effect=fake_ctor), \
             mock.patch.object(gdb, 'reap_gdb_zombies'), \
             mock.patch.object(gdb.time, 'sleep'):
            with self.assertRaises(gdb.DebuggerNotConnectedError):
                gdb.get_controller(device='NRF52840_XXAA', max_retries=3)

        self.assertEqual(len(created), 3, 'expected one controller per retry')
        for ctrl in created:
            ctrl.exit.assert_called_once()
        self.assertEqual(gdb._gdb_controller_cache, {},
                         'failed controllers must not be cached')

    def test_successful_controller_is_not_discarded(self):
        """A controller that connects cleanly is cached and left running."""
        ctrl = mock.MagicMock(name='GdbController')
        ctrl.get_gdb_response.return_value = []
        ctrl.write.return_value = []  # no error items in `tar ext` response

        with mock.patch.object(gdb, 'GdbController', return_value=ctrl), \
             mock.patch.object(gdb, 'reap_gdb_zombies'), \
             mock.patch.object(gdb.time, 'sleep'):
            result = gdb.get_controller(device='NRF52840_XXAA', max_retries=3)

        self.assertIs(result, ctrl)
        ctrl.exit.assert_not_called()
        self.assertIn(('NRF52840_XXAA', '127.0.0.1', 2331),
                      gdb._gdb_controller_cache)
        # A clean connect keeps non-stop; RTT detection relies on this flag to
        # decide it does NOT need to resume the core after its memory reads.
        self.assertIs(result.lager_non_stop, True)

    @staticmethod
    def _ctor_factory(created, error_item_for, *, only_first=False):
        """Build a GdbController constructor stub.

        ``error_item_for`` is the error dict a controller's ``tar ext`` returns;
        when ``only_first`` is set only the first controller returns it (the
        rest connect cleanly), modelling a successful fallback.
        """
        def fake_ctor(*_args, **_kwargs):
            idx = len(created)
            ctrl = mock.MagicMock(name=f'GdbController{idx}')
            ctrl.get_gdb_response.return_value = []

            def write(cmd, *_a, **_k):
                if cmd.startswith('tar ext') and (not only_first or idx == 0):
                    return [error_item_for]
                return []

            ctrl.write.side_effect = write
            created.append(ctrl)
            return ctrl
        return fake_ctor

    @staticmethod
    def _written(ctrl):
        return [c.args[0] for c in ctrl.write.call_args_list]

    def test_non_stop_rejection_falls_back_to_all_stop(self):
        """JLinkGDBServer rejecting non-stop must transparently downgrade to
        all-stop and reconnect once — no error raised, no attempt/sleep burned."""
        nonstop_err = {
            'type': 'result', 'message': 'error',
            'payload': {'msg': 'Non-stop mode requested, but remote does not support non-stop'},
        }
        created = []
        with mock.patch.object(gdb, 'GdbController',
                               side_effect=self._ctor_factory(created, nonstop_err, only_first=True)), \
             mock.patch.object(gdb, 'reap_gdb_zombies'), \
             mock.patch.object(gdb.time, 'sleep') as sleep_mock:
            result = gdb.get_controller(device='NRF52840_XXAA', max_retries=3)

        # Exactly one fallback: first controller discarded, second cached/returned.
        self.assertEqual(len(created), 2, 'expected one all-stop fallback reconnect')
        self.assertIs(result, created[1])
        created[0].exit.assert_called_once()
        created[1].exit.assert_not_called()
        # The fallback is free — no inter-attempt sleep.
        sleep_mock.assert_not_called()
        # First tried non-stop; the fallback controller must NOT request it.
        self.assertIn('set non-stop on', self._written(created[0]))
        self.assertNotIn('set non-stop on', self._written(created[1]))
        self.assertTrue(any(c.startswith('tar ext') for c in self._written(created[1])))
        self.assertIn(('NRF52840_XXAA', '127.0.0.1', 2331), gdb._gdb_controller_cache)
        # The fallback controller is in all-stop; the recorded flag lets RTT
        # detection know it must resume the core after its halting memory reads.
        self.assertIs(created[1].lager_non_stop, False)

    def test_genuine_target_error_still_raises_and_retries(self):
        """A non-(non-stop) `target` error is NOT swallowed: it retries the full
        ladder and ultimately raises (loud failure preserved)."""
        genuine_err = {
            'type': 'result', 'message': 'error',
            'payload': {'msg': 'Remote communication error: Connection refused'},
        }
        created = []
        with mock.patch.object(gdb, 'GdbController',
                               side_effect=self._ctor_factory(created, genuine_err)), \
             mock.patch.object(gdb, 'reap_gdb_zombies'), \
             mock.patch.object(gdb.time, 'sleep'):
            with self.assertRaises(gdb.DebuggerNotConnectedError):
                gdb.get_controller(device='NRF52840_XXAA', max_retries=3)

        # Genuine errors consume every attempt (no free fallback).
        self.assertEqual(len(created), 3)
        for ctrl in created:
            ctrl.exit.assert_called_once()
        self.assertEqual(gdb._gdb_controller_cache, {})


if __name__ == '__main__':
    unittest.main()
