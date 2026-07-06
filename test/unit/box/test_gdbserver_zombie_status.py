# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Regression tests for defunct/zombie gdbserver detection.

A ``JLinkGDBServer`` left ``<defunct>`` by a flash that ran while the J-Link
probe was down passes a bare ``os.kill(pid, 0)`` check (a zombie lingers in the
process table until its parent reaps it). Before this fix, both
``check_process`` and ``get_jlink_gdbserver_status`` reported such a zombie as
"running", so ``connect_jlink(ignore_if_connected=True)`` returned early and
reused the dead server, failing the next flash. These tests pin the corrected
behaviour: a zombie reads as not-running so the caller force-restarts a clean
server.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, mock_open, MagicMock


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted):
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


# Importing lager.debug runs the package __init__, which pulls in api/gdb and a
# pile of hardware libraries. Stub them so this pure-logic test needs no deps.
for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'pexpect', 'pexpect.replwrap', 'pexpect.exceptions',
    'pygdbmi', 'pygdbmi.gdbcontroller', 'pygdbmi.constants',
]:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.debug import mappings as mappings_mod  # noqa: E402
from lager.debug import gdbserver as gdbserver_mod  # noqa: E402


def _stat(state, comm='JLinkGDBServerC'):
    """A plausible /proc/<pid>/stat line: 'pid (comm) state ...'."""
    return f'4242 ({comm}) {state} 1 4242 4242 0 -1 4194560 123 0 0 0\n'


class CheckProcessZombie(unittest.TestCase):
    def test_live_process_is_running(self):
        with patch.object(mappings_mod.os, 'kill', return_value=None), \
             patch('builtins.open', mock_open(read_data=_stat('S'))):
            self.assertTrue(mappings_mod.check_process(4242))

    def test_zombie_process_is_not_running(self):
        with patch.object(mappings_mod.os, 'kill', return_value=None), \
             patch('builtins.open', mock_open(read_data=_stat('Z'))):
            self.assertFalse(mappings_mod.check_process(4242))

    def test_nonexistent_process_is_not_running(self):
        with patch.object(mappings_mod.os, 'kill', side_effect=OSError):
            # /proc is never consulted when the process does not exist.
            self.assertFalse(mappings_mod.check_process(4242))

    def test_proc_unavailable_falls_back_to_kill(self):
        # Non-Linux host (no /proc): os.kill succeeded, so assume alive.
        with patch.object(mappings_mod.os, 'kill', return_value=None), \
             patch('builtins.open', side_effect=OSError):
            self.assertTrue(mappings_mod.check_process(4242))

    def test_comm_with_spaces_and_parens_is_parsed(self):
        # comm can contain spaces/parens; state is the token after the LAST ')'.
        stat = _stat('Z', comm='J Link (GDB) Srv')
        with patch.object(mappings_mod.os, 'kill', return_value=None), \
             patch('builtins.open', mock_open(read_data=stat)):
            self.assertFalse(mappings_mod.check_process(4242))


class GdbserverStatusZombie(unittest.TestCase):
    def setUp(self):
        fd, self.pidfile = tempfile.mkstemp(prefix='jlink_gdbserver_', suffix='.pid')
        os.write(fd, b'4242')
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(self.pidfile) and os.remove(self.pidfile))
        self._orig = gdbserver_mod.jlink_gdbserver_pidfile
        gdbserver_mod.jlink_gdbserver_pidfile = lambda serial: self.pidfile
        self.addCleanup(
            lambda: setattr(gdbserver_mod, 'jlink_gdbserver_pidfile', self._orig)
        )

    def test_zombie_reports_not_running_and_removes_pidfile(self):
        with patch.object(gdbserver_mod, 'check_process', return_value=False):
            status = gdbserver_mod.get_jlink_gdbserver_status(serial='000051014439')
        self.assertEqual(status, {'running': False, 'pid': None})
        self.assertFalse(
            os.path.exists(self.pidfile),
            'stale pidfile should be removed so connect() restarts a fresh server',
        )

    def test_live_process_reports_running_and_keeps_pidfile(self):
        with patch.object(gdbserver_mod, 'check_process', return_value=True):
            status = gdbserver_mod.get_jlink_gdbserver_status(serial='000051014439')
        self.assertEqual(status, {'running': True, 'pid': 4242})
        self.assertTrue(os.path.exists(self.pidfile))


if __name__ == '__main__':
    unittest.main()
