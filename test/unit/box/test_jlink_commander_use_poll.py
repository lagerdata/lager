# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit test for box/lager/debug/jlink.py — the commander() context manager must
spawn JLinkExe with use_poll=True.

The debug service is a long-lived process. Once it accumulates >= 1024 open
file descriptors, a newly spawned JLinkExe child PTY is assigned an fd number
>= FD_SETSIZE (1024). pexpect's REPLWrapper then calls select(), which cannot
represent fds >= 1024 and raises "ValueError: filedescriptor out of range in
select()" — surfacing as a 500 on /debug/erase and /debug/flash. poll() has no
such ceiling, so commander() must pass use_poll=True to pexpect.spawn().

jlink.py is loaded directly via importlib so this test doesn't pull in the full
lager.debug package (which imports hardware drivers / pyvisa).
"""

import importlib.util
import os
import unittest
from unittest import mock


HERE = os.path.dirname(__file__)
JLINK_PATH = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'debug', 'jlink.py')
)


def _load_jlink():
    spec = importlib.util.spec_from_file_location('jlink', JLINK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jlink = _load_jlink()


class CommanderUsePollTests(unittest.TestCase):
    def test_spawn_called_with_use_poll(self):
        """commander() must spawn JLinkExe with use_poll=True (not select())."""
        fake_child = mock.MagicMock()
        fake_repl = mock.MagicMock()

        with mock.patch.object(jlink, 'get_jlink_exe_path',
                               return_value='/usr/bin/JLinkExe'), \
             mock.patch.object(jlink.pexpect, 'spawn',
                               return_value=fake_child) as mock_spawn, \
             mock.patch.object(jlink.replwrap, 'REPLWrapper',
                               return_value=fake_repl):
            with jlink.commander(['-device', 'DA14695']) as jl:
                self.assertIs(jl, fake_repl)

        mock_spawn.assert_called_once()
        self.assertTrue(
            mock_spawn.call_args.kwargs.get('use_poll'),
            'pexpect.spawn must be called with use_poll=True so a JLinkExe child '
            'PTY fd >= 1024 does not crash select() in REPLWrapper',
        )


if __name__ == '__main__':
    unittest.main()
