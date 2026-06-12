# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the DA1469x opt-in uncached post-program verify in
box/lager/debug/jlink.py (LAGER_DA1469_UNCACHED_VERIFY), plus the
_parse_mem8_bytes / _iter_loadfile_cmds refactor it rides on.

The cached-XIP loadfile compare can report a false "Verification failed" on a
no-reset attach; the feature re-checks through the uncached QSPI mirror. These
tests pin three contracts:

  1. Flag off / non-DA1469x device: flash() output is byte-identical to the
     pre-feature path and no w4/mem8 commands are issued (regression guard).
  2. Flag on + read-back matches: the false failure line is stripped and the
     success note carries no failure-marker substring.
  3. Flag on + genuine mismatch / inconclusive read-back: the original output
     survives unmodified and failure is still detectable by marker-grep.

jlink.py is loaded directly via importlib so this test doesn't pull in the full
lager.debug package (which imports hardware drivers / pyvisa).
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
    spec = importlib.util.spec_from_file_location('jlink_uncached_verify_mod', JLINK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jlink = _load_jlink()

XIP = jlink._DA1469X_QSPI_XIP_START          # 0x16000000
MIRROR_OFF = jlink._DA1469X_QSPI_UNCACHED_OFFSET  # 0x20000000
CHUNK = jlink._UNCACHED_VERIFY_CHUNK


class FakeRepl:
    """Stands in for the replwrap REPL: records commands, replies via responder."""

    def __init__(self, responder):
        self.commands = []
        self._responder = responder

    def run_command(self, cmd):
        self.commands.append(cmd)
        return self._responder(cmd)


def mem8_text(addr, data):
    """Format bytes the way J-Link Commander prints mem8 output, including the
    echoed command line (no '=') and a trailing prompt fragment."""
    lines = [f'mem8 {hex(addr)} {len(data)}\r']
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        lines.append('{:08X} = {} \r'.format(addr + off, ' '.join(f'{b:02X}' for b in row)))
    lines.append('J-Link>')
    return '\n'.join(lines)


def make_responder(flash_bytes, mirror_base, loadfile_out='LOADFILE-OK\r\nDone.\r'):
    """Responder backed by a fake flash image mapped at the uncached mirror."""
    def responder(cmd):
        if cmd.startswith('mem8 '):
            _, addr_s, n_s = cmd.split()
            addr, n = int(addr_s, 16), int(n_s)
            start = addr - mirror_base
            return mem8_text(addr, flash_bytes[start:start + n])
        if cmd.startswith('loadfile '):
            return loadfile_out
        return f'{cmd.upper()}-OUT'
    return responder


@contextlib.contextmanager
def env(**values):
    """Set/clear the two feature env vars around a test body."""
    with mock.patch.dict(os.environ):
        for key in ('LAGER_DA1469_UNCACHED_VERIFY', 'LAGER_DA1469_UNCACHED_VERIFY_BYTES'):
            os.environ.pop(key, None)
        os.environ.update(values)
        yield


def make_jlink(device):
    """JLink instance without __init__ (avoids exe discovery); flash() only
    needs args/script_file/serial."""
    jl = jlink.JLink.__new__(jlink.JLink)
    jl.args = ['-device', device, '-if', 'SWD']
    jl.script_file = None
    jl.serial = None
    return jl


def run_flash(device, responder, binfiles=(), hexfiles=(), elffiles=()):
    """Drive JLink.flash() against a FakeRepl; return (yields, commands)."""
    fake = FakeRepl(responder)

    @contextlib.contextmanager
    def fake_commander(args, script_file=None, serial=None):
        yield fake

    with mock.patch.object(jlink, 'commander', fake_commander):
        out = list(make_jlink(device).flash((list(hexfiles), list(binfiles), list(elffiles))))
    return out, fake.commands


class TempBinMixin:
    def make_bin(self, data):
        f = tempfile.NamedTemporaryFile(suffix='.bin', delete=False)
        self.addCleanup(os.unlink, f.name)
        f.write(data)
        f.close()
        return f.name


class FlagOffRegressionTests(TempBinMixin, unittest.TestCase):
    """Flag unset/off and non-DA1469x devices must be byte-identical to the
    pre-feature path, with no verify commands issued."""

    def test_flag_off_yields_byte_identical(self):
        data = bytes(range(256)) * 4
        path = self.make_bin(data)
        responder = make_responder(data, XIP + MIRROR_OFF)
        expected = ['CONNECT-OUT', 'RNH-OUT', 'H-OUT', 'LOADFILE-OK\r\nDone.\r']
        for flag in (None, '0', 'false', 'no', 'off', ''):
            with self.subTest(flag=flag), env(**({} if flag is None else
                                                 {'LAGER_DA1469_UNCACHED_VERIFY': flag})):
                yields, commands = run_flash('DA14695', responder, binfiles=[(path, XIP)])
                self.assertEqual(yields, expected)
                self.assertFalse([c for c in commands
                                  if c.startswith('w4 ') or c.startswith('mem8 ')])

    def test_non_da1469_device_ignores_flag(self):
        data = b'\xAA' * 64
        path = self.make_bin(data)
        with env(LAGER_DA1469_UNCACHED_VERIFY='1'):
            yields, commands = run_flash('NRF52840_XXAA',
                                         make_responder(data, XIP + MIRROR_OFF),
                                         binfiles=[(path, XIP)])
        # No DA1469x gate at all: no rnh/h, no verify commands.
        self.assertEqual(yields, ['CONNECT-OUT', 'LOADFILE-OK\r\nDone.\r'])
        self.assertFalse([c for c in commands
                          if c.startswith('w4 ') or c.startswith('mem8 ')])


class GeneratorParityTests(TempBinMixin, unittest.TestCase):
    """The verify generator must be exactly the default generator for files it
    doesn't touch (hex, elf, out-of-window bins)."""

    def test_parity_for_out_of_scope_files(self):
        out_bin = self.make_bin(b'\x01' * 32)
        cases = {
            'hex+elf': dict(hexfiles=['/x/a.hex'], binfiles=[], elffiles=['/x/b.elf']),
            'bin below window': dict(hexfiles=[], binfiles=[(out_bin, 0x0)], elffiles=[]),
            'bin past window': dict(hexfiles=[], binfiles=[(out_bin, jlink._DA1469X_QSPI_XIP_END + 1)],
                                    elffiles=[]),
        }
        for name, files in cases.items():
            with self.subTest(case=name):
                responder = make_responder(b'', XIP + MIRROR_OFF)
                a, b = FakeRepl(responder), FakeRepl(responder)
                default = list(jlink._yield_loadfile_outputs(
                    a, files['hexfiles'], files['binfiles'], files['elffiles']))
                verify = list(jlink._yield_loadfile_outputs_uncached_verify(
                    b, files['hexfiles'], files['binfiles'], files['elffiles']))
                self.assertEqual(verify, default)
                self.assertEqual(b.commands, a.commands)


class MatchTests(TempBinMixin, unittest.TestCase):
    FALSE_FAIL = ('Downloading file [img.bin]...\r\n'
                  'Verification failed @ address 0x16000000.\r\n'
                  'O.K.\r')

    def test_match_strips_marker_and_notes(self):
        data = bytes((i * 7) & 0xFF for i in range(1000))
        path = self.make_bin(data)
        responder = make_responder(data, XIP + MIRROR_OFF, loadfile_out=self.FALSE_FAIL)
        with env(LAGER_DA1469_UNCACHED_VERIFY='1'):
            yields, commands = run_flash('DA14695', responder, binfiles=[(path, XIP)])
        joined = '\n'.join(yields).lower()
        self.assertNotIn('verification failed', joined)
        self.assertNotIn('verify failed', joined)
        note = [y for y in yields if y.startswith('Uncached read-back OK')]
        self.assertEqual(len(note), 1)
        self.assertIn('1000 of 1000 bytes', note[0])
        # Flush must precede the first mirror read, which starts at the mirror base.
        w4 = commands.index(f'w4 {hex(jlink._DA1469X_CACHE_CTRL1_REG)} 1')
        first_mem8 = next(i for i, c in enumerate(commands) if c.startswith('mem8 '))
        self.assertLess(w4, first_mem8)
        self.assertTrue(commands[first_mem8].startswith(f'mem8 {hex(XIP + MIRROR_OFF)} '))

    def test_match_without_marker_is_identity(self):
        data = b'\x5A' * 128
        path = self.make_bin(data)
        clean = 'Downloading file [img.bin]...\r\nO.K.\r'
        responder = make_responder(data, XIP + MIRROR_OFF, loadfile_out=clean)
        with env(LAGER_DA1469_UNCACHED_VERIFY='1'):
            yields, _ = run_flash('DA14695', responder, binfiles=[(path, XIP)])
        # connect/rnh/h, then the untouched capture, then the note.
        self.assertEqual(yields[3], clean)


class MismatchAndInconclusiveTests(TempBinMixin, unittest.TestCase):
    def test_mismatch_second_chunk_reports_address(self):
        data = bytes((i * 3) & 0xFF for i in range(2 * CHUNK))
        path = self.make_bin(data)
        corrupted = bytearray(data)
        corrupted[5000] ^= 0xFF
        responder = make_responder(bytes(corrupted), XIP + MIRROR_OFF)
        with env(LAGER_DA1469_UNCACHED_VERIFY='1'):
            yields, commands = run_flash('DA14695', responder, binfiles=[(path, XIP)])
        mem8s = [c for c in commands if c.startswith('mem8 ')]
        self.assertEqual(len(mem8s), 2)  # first chunk matched, second stopped the scan
        self.assertIn('LOADFILE-OK\r\nDone.\r', yields)  # capture unmodified
        fail = [y for y in yields if 'Verification failed @' in y]
        self.assertEqual(len(fail), 1)
        self.assertIn(hex(XIP + 5000), fail[0])  # 0x16001388

    def test_cap_env_respected(self):
        data = b'\xC3' * (3 * CHUNK)
        path = self.make_bin(data)
        responder = make_responder(data, XIP + MIRROR_OFF)
        with env(LAGER_DA1469_UNCACHED_VERIFY='1',
                 LAGER_DA1469_UNCACHED_VERIFY_BYTES=str(CHUNK)):
            yields, commands = run_flash('DA14695', responder, binfiles=[(path, XIP)])
        mem8s = [c for c in commands if c.startswith('mem8 ')]
        self.assertEqual(mem8s, [f'mem8 {hex(XIP + MIRROR_OFF)} {CHUNK}'])
        note = [y for y in yields if y.startswith('Uncached read-back OK')]
        self.assertIn(f'{CHUNK} of {3 * CHUNK} bytes', note[0])

    def test_unreadable_file_fails_safe(self):
        responder = make_responder(b'', XIP + MIRROR_OFF)
        with env(LAGER_DA1469_UNCACHED_VERIFY='1'):
            yields, commands = run_flash('DA14695', responder,
                                         binfiles=[('/nonexistent/img.bin', XIP)])
        self.assertIn('LOADFILE-OK\r\nDone.\r', yields)
        warn = [y for y in yields if 'uncached verify skipped' in y]
        self.assertEqual(len(warn), 1)
        self.assertNotIn('verification failed', '\n'.join(yields).lower())
        # File read precedes the flush: nothing was issued.
        self.assertFalse([c for c in commands
                          if c.startswith('w4 ') or c.startswith('mem8 ')])

    def test_short_read_inconclusive_preserves_failure_line(self):
        data = b'\x11' * 64
        path = self.make_bin(data)
        segger_fail = 'Writing target memory failed.\r\nVerification failed @ address 0x16000000.\r'

        def responder(cmd):
            if cmd.startswith('mem8 '):
                return 'no data lines here'
            if cmd.startswith('loadfile '):
                return segger_fail
            return f'{cmd.upper()}-OUT'

        with env(LAGER_DA1469_UNCACHED_VERIFY='1'):
            yields, _ = run_flash('DA14695', responder, binfiles=[(path, XIP)])
        self.assertIn(segger_fail, yields)  # original output untouched
        warn = [y for y in yields if 'uncached verify could not complete' in y]
        self.assertEqual(len(warn), 1)
        self.assertNotIn('verification failed', warn[0].lower())

    def test_skip_warning_follows_verdict(self):
        data = b'\x44' * 32
        path = self.make_bin(data)
        skipped = 'Flash programming skipped (contents already match)\r'
        responder = make_responder(data, XIP + MIRROR_OFF, loadfile_out=skipped)
        with env(LAGER_DA1469_UNCACHED_VERIFY='1'):
            yields, _ = run_flash('DA14695', responder, binfiles=[(path, XIP)])
        note_i = next(i for i, y in enumerate(yields) if y.startswith('Uncached read-back OK'))
        warn_i = yields.index(jlink._LOADFILE_SKIPPED_MSG)
        self.assertLess(note_i, warn_i)


class ParseMem8BytesTests(unittest.TestCase):
    def test_parses_echo_prompt_and_crlf(self):
        data = bytes(range(40))
        out = mem8_text(0x36000000, data)
        self.assertEqual(jlink._parse_mem8_bytes(out, 40), data)

    def test_trims_to_length(self):
        out = mem8_text(0x36000000, bytes(range(32)))
        self.assertEqual(jlink._parse_mem8_bytes(out, 16), bytes(range(16)))

    def test_short_or_garbled_returns_fewer(self):
        self.assertEqual(jlink._parse_mem8_bytes('no equals sign', 8), b'')
        out = mem8_text(0x36000000, b'\x01\x02')
        self.assertEqual(len(jlink._parse_mem8_bytes(out, 8)), 2)


if __name__ == '__main__':
    unittest.main()
