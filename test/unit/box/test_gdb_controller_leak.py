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


if __name__ == '__main__':
    unittest.main()
