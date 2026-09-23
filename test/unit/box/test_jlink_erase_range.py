# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The Commander command sequence ``JLink.chip_erase`` issues for each source of
an erase range: the family default, a ``LAGER_ERASE_RANGE`` script line, and
an explicit ``start`` / ``length`` from ``--erase-start`` / ``--erase-size``.

Pinned per case, exactly:

* a DA1469x keeps its prelude (``Exec SetEnableFlashbank 0x16000000=1``,
  ``Exec EnableEraseAllFlashBanks``) and never runs a bare ``erase``;
* a request replaces both the default and the script line, and on another
  part replaces the bare full-chip ``erase`` with ``erase <start> <end>``;
* the ``erase`` command carries a timeout that scales with the range, since
  ``commander()`` spawns JLinkExe with pexpect's default 30 s per command.

``jlink.py`` is loaded standalone by path, as its other tests do, and the
REPL is a fake that records each command with the timeout it was given.
"""

import contextlib
import importlib.util
import os
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(__file__)
JLINK_PATH = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'debug', 'jlink.py')
)


def _load_jlink():
    spec = importlib.util.spec_from_file_location('jlink_erase_range_mod', JLINK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jlink = _load_jlink()

XIP = jlink._DA1469X_QSPI_XIP_START  # 0x16000000
MIB = 1 << 20
PRELUDE = ['connect', 'Exec SetEnableFlashbank 0x16000000=1', 'Exec EnableEraseAllFlashBanks']


class FakeRepl:
    """Records ``(command, timeout)``; answers every command with a marker."""

    def __init__(self):
        self.commands = []
        self.timeouts = []

    def run_command(self, cmd, timeout=-1):
        self.commands.append(cmd)
        self.timeouts.append(timeout)
        return f'{cmd}-OUT'


def make_jlink(device, script_file=None):
    jl = jlink.JLink.__new__(jlink.JLink)
    jl.args = ['-device', device, '-if', 'SWD', '-speed', '4000']
    jl.script_file = script_file
    jl.serial = None
    return jl


def run_chip_erase(device, script_file=None, **kwargs):
    fake = FakeRepl()

    @contextlib.contextmanager
    def fake_commander(args, script_file=None, serial=None):
        yield fake

    with mock.patch.object(jlink, 'commander', fake_commander):
        out = list(make_jlink(device, script_file).chip_erase(**kwargs))
    return out, fake


class ChipEraseCommandTests(unittest.TestCase):

    def _script(self, text):
        handle = tempfile.NamedTemporaryFile('w', suffix='.JLinkScript', delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_da1469x_default_is_one_mib_at_the_xip_base(self):
        out, fake = run_chip_erase('DA14695')
        self.assertEqual(fake.commands, PRELUDE + ['erase 0x16000000 0x160fffff'])
        self.assertEqual(fake.timeouts[-1], 30)
        self.assertEqual(out, [f'{c}-OUT' for c in fake.commands])

    def test_da1469x_script_line_sets_the_range(self):
        script = self._script('LAGER_ERASE_RANGE: 0x16000000 0x161FFFFF\n')
        _, fake = run_chip_erase('DA14695', script)
        self.assertEqual(fake.commands, PRELUDE + ['erase 0x16000000 0x161fffff'])
        self.assertEqual(fake.timeouts[-1], 60)

    def test_da1469x_request_wins_over_the_script_line(self):
        script = self._script('LAGER_ERASE_RANGE: 0x16000000 0x161FFFFF\n')
        _, fake = run_chip_erase('DA14695', script, start=XIP + MIB, length=MIB)
        self.assertEqual(fake.commands, PRELUDE + ['erase 0x16100000 0x161fffff'])
        self.assertEqual(fake.timeouts[-1], 30)

    def test_da1469x_request_keeps_the_prelude(self):
        _, fake = run_chip_erase('DA14695', start=XIP, length=4 * MIB)
        self.assertEqual(fake.commands[:3], PRELUDE)
        self.assertEqual(fake.commands[3], 'erase 0x16000000 0x163fffff')
        self.assertEqual(fake.timeouts[3], 120)

    def test_da1469x_never_runs_a_bare_erase(self):
        for kwargs in ({}, {'start': XIP, 'length': MIB}):
            with self.subTest(kwargs=kwargs):
                _, fake = run_chip_erase('DA14695', **kwargs)
                self.assertNotIn('erase', fake.commands)

    def test_another_part_with_no_request_erases_the_whole_chip(self):
        out, fake = run_chip_erase('NRF52840_XXAA')
        self.assertEqual(fake.commands, ['connect', 'erase'])
        self.assertEqual(fake.timeouts, [-1, -1], 'the full-chip erase keeps its default wait')
        self.assertEqual(out, ['connect-OUT', 'erase-OUT'])

    def test_another_part_with_a_request_erases_that_range_only(self):
        _, fake = run_chip_erase('NRF52840_XXAA', start=0x08000000, length=2 * MIB)
        self.assertEqual(fake.commands, ['connect', 'erase 0x8000000 0x81fffff'])
        self.assertEqual(fake.timeouts, [-1, 60])

    def test_a_script_line_does_nothing_on_another_part(self):
        script = self._script('LAGER_ERASE_RANGE: 0x16000000 0x161FFFFF\n')
        _, fake = run_chip_erase('NRF52840_XXAA', script)
        self.assertEqual(fake.commands, ['connect', 'erase'])


if __name__ == '__main__':
    unittest.main()
