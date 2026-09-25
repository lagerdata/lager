# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
commander() (box/lager/debug/jlink.py) when JLinkExe goes away.

commander() caught ``pexpect.exceptions.EOF`` around its whole body so that the
EOF ``q`` causes would not escape. That also caught the EOF a command raises
when JLinkExe exits under it -- and a @contextmanager that swallows an
exception thrown in at its ``yield`` suppresses it. On a bench where a second
J-Link client drove the probe off USB mid-flash, the flash returned no output
and no error, and ``lager debug flash`` printed "Flashed!" over an erased
part. JLinkExe exiting before its prompt was the other half: commander()
returned without yielding, and callers saw contextlib's "generator didn't
yield", which says nothing about the probe.

Pinned: both now raise ``JLinkCommanderExited`` with what JLinkExe printed
last, and the EOF from ``q`` is still absorbed.
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
    spec = importlib.util.spec_from_file_location('jlink_commander_exit', JLINK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jlink = _load_jlink()
EOF = jlink.pexpect.exceptions.EOF


class CommanderExitTests(unittest.TestCase):
    def _patched(self, repl):
        child = mock.MagicMock()
        child.before = 'Connecting to J-Link via USB...FAILED\r\n'
        return (
            mock.patch.object(jlink, 'get_jlink_exe_path', return_value='/usr/bin/JLinkExe'),
            mock.patch.object(jlink.pexpect, 'spawn', return_value=child),
            mock.patch.object(jlink.replwrap, 'REPLWrapper', **repl),
        )

    def _enter(self, repl, body):
        a, b, c = self._patched(repl)
        with a, b, c:
            with jlink.commander(['-device', 'DA14695']) as jl:
                body(jl)

    def test_exit_before_the_prompt_is_named(self):
        with self.assertRaises(jlink.JLinkCommanderExited) as caught:
            self._enter({'side_effect': EOF('gone')}, lambda jl: None)
        message = str(caught.exception)
        self.assertTrue(message.startswith(jlink.COMMANDER_EXITED + ' before its prompt'))
        self.assertIn('Connecting to J-Link via USB...FAILED', message)

    def test_exit_mid_session_is_raised_not_swallowed(self):
        repl = mock.MagicMock()
        repl.run_command.side_effect = EOF('gone')
        with self.assertRaises(jlink.JLinkCommanderExited) as caught:
            self._enter({'return_value': repl}, lambda jl: jl.run_command('loadfile x.bin'))
        self.assertTrue(str(caught.exception).startswith(
            jlink.COMMANDER_EXITED + ' mid-session'))

    def test_the_eof_quit_causes_is_absorbed(self):
        repl = mock.MagicMock()
        repl.run_command.side_effect = lambda cmd: (_ for _ in ()).throw(EOF('bye')) \
            if cmd == 'q' else 'O.K.'
        self._enter({'return_value': repl}, lambda jl: jl.run_command('connect'))
        self.assertEqual([c.args[0] for c in repl.run_command.call_args_list],
                         ['connect', 'q'])

    def test_other_errors_in_the_body_still_propagate(self):
        repl = mock.MagicMock()
        with self.assertRaises(ValueError):
            self._enter({'return_value': repl}, lambda jl: (_ for _ in ()).throw(ValueError('x')))


if __name__ == '__main__':
    unittest.main()
