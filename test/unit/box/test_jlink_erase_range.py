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
        self.assertEqual(fake.timeouts[-1], 60)
        self.assertEqual(out, [f'{c}-OUT' for c in fake.commands])

    def test_da1469x_script_line_sets_the_range(self):
        script = self._script('LAGER_ERASE_RANGE: 0x16000000 0x161FFFFF\n')
        _, fake = run_chip_erase('DA14695', script)
        self.assertEqual(fake.commands, PRELUDE + ['erase 0x16000000 0x161fffff'])
        self.assertEqual(fake.timeouts[-1], 120)

    def test_da1469x_request_wins_over_the_script_line(self):
        script = self._script('LAGER_ERASE_RANGE: 0x16000000 0x161FFFFF\n')
        _, fake = run_chip_erase('DA14695', script, start=XIP + MIB, length=MIB)
        self.assertEqual(fake.commands, PRELUDE + ['erase 0x16100000 0x161fffff'])
        self.assertEqual(fake.timeouts[-1], 60)

    def test_da1469x_request_keeps_the_prelude(self):
        _, fake = run_chip_erase('DA14695', start=XIP, length=4 * MIB)
        self.assertEqual(fake.commands[:3], PRELUDE)
        self.assertEqual(fake.commands[3], 'erase 0x16000000 0x163fffff')
        self.assertEqual(fake.timeouts[3], 240)

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
        self.assertEqual(fake.timeouts, [-1, 120])

    def test_a_script_line_does_nothing_on_another_part(self):
        script = self._script('LAGER_ERASE_RANGE: 0x16000000 0x161FFFFF\n')
        _, fake = run_chip_erase('NRF52840_XXAA', script)
        self.assertEqual(fake.commands, ['connect', 'erase'])



class Nrf5340Repl(FakeRepl):
    """Answers ``mem32 <addr> 1`` the way Commander does, from *words*."""

    def __init__(self, words=None, unreadable=()):
        super().__init__()
        self.words = words or {}
        self.unreadable = set(unreadable)

    def run_command(self, cmd, timeout=-1):
        self.commands.append(cmd)
        self.timeouts.append(timeout)
        if cmd.startswith('mem32 '):
            address = int(cmd.split()[1], 16)
            if address in self.unreadable:
                return 'Could not read memory.'
            return f'{address:08X} = {self.words.get(address, 0xFFFFFFFF):08X}'
        return f'{cmd}-OUT'


def run_nrf5340_erase(repl, device='NRF5340_XXAA_APP', **kwargs):
    @contextlib.contextmanager
    def fake_commander(args, script_file=None, serial=None):
        yield repl

    with mock.patch.object(jlink, 'commander', fake_commander):
        return list(make_jlink(device).chip_erase(**kwargs))


class Nrf5340EraseTests(unittest.TestCase):
    """An nRF5340 app core erases its 1 MiB of flash by range, then reads it back.

    A bare ``erase`` on this part can print ``Erasing done.`` and erase
    nothing, so it never runs one. The UICR is outside the range.
    """

    READBACK = ['mem32 0x0 1', 'mem32 0x80000 1', 'mem32 0xffffc 1']

    def test_no_request_erases_the_app_flash_by_range(self):
        repl = Nrf5340Repl()
        out = run_nrf5340_erase(repl)
        self.assertEqual(repl.commands, ['connect', 'erase 0x0 0xfffff'] + self.READBACK)
        self.assertEqual(repl.timeouts[1], 60)
        self.assertNotIn('erase', repl.commands)
        self.assertFalse([line for line in out if jlink.ERASE_READBACK_FAILED in line])

    def test_a_word_that_is_not_ff_fails_the_erase(self):
        repl = Nrf5340Repl(words={0x80000: 0x20001234})
        out = run_nrf5340_erase(repl)
        self.assertEqual(
            out[-1],
            f'Erase check: 0x00080000 reads 0x20001234 {jlink.ERASE_READBACK_FAILED}')
        self.assertEqual(repl.commands[-1], 'mem32 0x80000 1', 'it stops at the first bad word')

    def test_a_word_that_cannot_be_read_fails_the_erase(self):
        repl = Nrf5340Repl(unreadable={0x0})
        out = run_nrf5340_erase(repl)
        self.assertEqual(
            out[-1],
            f'Erase check: 0x00000000 could not be read {jlink.ERASE_READBACK_FAILED}')

    def test_a_request_reads_back_its_own_range(self):
        repl = Nrf5340Repl()
        run_nrf5340_erase(repl, start=0x0, length=0x1000)
        self.assertEqual(repl.commands, [
            'connect', 'erase 0x0 0xfff',
            'mem32 0x0 1', 'mem32 0x800 1', 'mem32 0xffc 1',
        ])

    def test_the_network_core_keeps_the_bare_erase(self):
        repl = Nrf5340Repl()
        run_nrf5340_erase(repl, device='NRF5340_XXAA_NET')
        self.assertEqual(repl.commands, ['connect', 'erase'])

    def test_readback_addresses_stay_word_aligned_inside_the_range(self):
        self.assertEqual(jlink._readback_addresses(0x0, 0x100000), [0x0, 0x80000, 0xFFFFC])
        self.assertEqual(jlink._readback_addresses(0x1000, 4), [0x1000])
        self.assertEqual(jlink._readback_addresses(0x1002, 6), [0x1000, 0x1004])


if __name__ == '__main__':
    unittest.main()
