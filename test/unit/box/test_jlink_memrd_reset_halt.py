# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the DA1469x reset+halt-before-read behaviour in
box/lager/debug/jlink.py (JLink.read_memory).

A running DA1469x disables SWD and deep-sleeps shortly after boot, so a plain
``connect`` / ``mem8`` against live firmware fails. read_memory resets AND halts
at the reset vector first: for a DA1469x it runs ``r`` / ``h`` before ``mem8``
to catch the core before firmware can disable SWD (``rnh`` would let firmware
run, unlike flash() which needs the bootrom). These tests pin the gate:

  1. DA1469x, default env: r then h are issued before mem8 (never rnh).
  2. Non-DA1469x device: no r/h (regression guard for other parts).
  3. LAGER_DA1469_MEMRD_RESET_HALT opt-out: no r/h on a DA1469x.
  4. reset_halt= override wins over the env var in both directions.
  5. mem8 output still parses into bytes regardless of the reset+halt step.

jlink.py is loaded directly via importlib so this test doesn't pull in the full
lager.debug package (which imports hardware drivers / pyvisa).
"""

import contextlib
import importlib.util
import os
import unittest
from unittest import mock


HERE = os.path.dirname(__file__)
JLINK_PATH = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'debug', 'jlink.py')
)


def _load_jlink():
    spec = importlib.util.spec_from_file_location('jlink_memrd_reset_halt_mod', JLINK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jlink = _load_jlink()


class FakeRepl:
    """Stands in for the replwrap REPL: records commands, replies via responder."""

    def __init__(self, responder):
        self.commands = []
        self._responder = responder

    def run_command(self, cmd):
        self.commands.append(cmd)
        return self._responder(cmd)


def mem8_text(addr, data):
    """Format bytes the way J-Link Commander prints mem8 output."""
    lines = [f'mem8 {hex(addr)} {len(data)}\r']
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        lines.append('{:08X} = {} \r'.format(addr + off, ' '.join(f'{b:02X}' for b in row)))
    lines.append('J-Link>')
    return '\n'.join(lines)


def mem32_text(addr, data):
    """Format the way J-Link Commander prints mem32: one 8-hex-digit word VALUE
    per address. *data* is little-endian bytes (memory order)."""
    lines = [f'mem32 {hex(addr)} {len(data) // 4}\r']
    for off in range(0, len(data), 4):
        v = int.from_bytes(data[off:off + 4], 'little')
        lines.append('{:08X} = {:08X} \r'.format(addr + off, v))
    lines.append('J-Link>')
    return '\n'.join(lines)


def make_jlink(device):
    """JLink instance without __init__ (avoids exe discovery)."""
    jl = jlink.JLink.__new__(jlink.JLink)
    jl.args = ['-device', device, '-if', 'SWD', '-speed', '4000']
    jl.script_file = None
    jl.serial = None
    return jl


def run_read(device, data, address=0xE000ED00, length=4, **kwargs):
    """Drive JLink.read_memory() against a FakeRepl; return (result, commands).

    *data* is the little-endian byte sequence the device would expose at
    *address*; the responder serves it back through whichever of mem8/mem32
    read_memory chooses for the alignment.
    """
    def responder(cmd):
        if cmd.startswith('mem32 '):
            _, addr_s, n_s = cmd.split()
            return mem32_text(int(addr_s, 16), data[:int(n_s) * 4])
        if cmd.startswith('mem8 '):
            _, addr_s, n_s = cmd.split()
            return mem8_text(int(addr_s, 16), data[:int(n_s)])
        return f'{cmd.upper()}-OUT'

    fake = FakeRepl(responder)

    @contextlib.contextmanager
    def fake_commander(args, script_file=None, serial=None):
        yield fake

    with mock.patch.object(jlink, 'commander', fake_commander):
        result = make_jlink(device).read_memory(address, length, **kwargs)
    return result, fake.commands


@contextlib.contextmanager
def env(value=None):
    """Set or clear LAGER_DA1469_MEMRD_RESET_HALT around a test body."""
    with mock.patch.dict(os.environ):
        os.environ.pop('LAGER_DA1469_MEMRD_RESET_HALT', None)
        if value is not None:
            os.environ['LAGER_DA1469_MEMRD_RESET_HALT'] = value
        yield


# Cortex-M33 SCB CPUID, little-endian: reads back 0x410FD212.
CPUID_BYTES = bytes([0x12, 0xD2, 0x0F, 0x41])


class Da1469ResetHaltTests(unittest.TestCase):
    def test_da1469_resets_and_halts_before_mem8(self):
        with env():  # default (env unset) -> on
            result, commands = run_read('DA14695', CPUID_BYTES)
        self.assertEqual(result, CPUID_BYTES)
        self.assertEqual(commands[0], 'connect')
        # r (reset+halt) and h must precede the read; rnh must NOT be used
        # (it would let firmware run and disable SWD before the halt).
        self.assertNotIn('rnh', commands)
        r_i = commands.index('r')
        h_i = commands.index('h')
        read_i = next(i for i, c in enumerate(commands)
                      if c.startswith('mem8 ') or c.startswith('mem32 '))
        self.assertLess(r_i, h_i)
        self.assertLess(h_i, read_i)

    def test_non_da1469_does_not_reset_or_halt(self):
        with env():
            result, commands = run_read('NRF52840_XXAA', CPUID_BYTES)
        self.assertEqual(result, CPUID_BYTES)
        self.assertNotIn('r', commands)
        self.assertNotIn('rnh', commands)
        self.assertNotIn('h', commands)
        # 0xE000ED00/4 is word-aligned -> mem32 (one word).
        self.assertEqual(commands, ['connect', f'mem32 {hex(0xE000ED00)} 1'])

    def test_env_optout_skips_reset_halt_on_da1469(self):
        for value in ('0', 'false', 'no', 'off'):
            with self.subTest(value=value), env(value):
                result, commands = run_read('DA14695', CPUID_BYTES)
            self.assertEqual(result, CPUID_BYTES)
            self.assertNotIn('r', commands)
            self.assertNotIn('h', commands)

    def test_env_on_values_keep_reset_halt(self):
        for value in ('1', 'true', 'yes', 'on'):
            with self.subTest(value=value), env(value):
                _, commands = run_read('DA14695', CPUID_BYTES)
            self.assertIn('r', commands)
            self.assertIn('h', commands)
            self.assertNotIn('rnh', commands)

    def test_reset_halt_false_override_beats_default_env(self):
        with env():  # env on by default...
            _, commands = run_read('DA14695', CPUID_BYTES, reset_halt=False)
        self.assertNotIn('r', commands)  # ...but the explicit arg wins.
        self.assertNotIn('h', commands)

    def test_reset_halt_true_override_beats_env_optout(self):
        with env('0'):  # env opts out...
            _, commands = run_read('DA14695', CPUID_BYTES, reset_halt=True)
        self.assertIn('r', commands)  # ...but the explicit arg wins.
        self.assertIn('h', commands)
        self.assertNotIn('rnh', commands)

    def test_reset_halt_override_ignored_for_non_da1469(self):
        with env():
            _, commands = run_read('NRF52840_XXAA', CPUID_BYTES, reset_halt=True)
        self.assertNotIn('r', commands)
        self.assertNotIn('rnh', commands)
        self.assertNotIn('h', commands)


# Cortex-M0+ SCB CPUID, little-endian: reads back 0x410CC601.
KUMO_CPUID_BYTES = bytes([0x01, 0xC6, 0x0C, 0x41])


class WordVsByteAccessTests(unittest.TestCase):
    """SCS (e.g. CPUID @ 0xE000ED00) is word-access-only on ARMv6-M, so
    read_memory must use mem32 for word-aligned/whole-word reads and fall back
    to mem8 otherwise."""

    def _read_cmd(self, commands):
        return next(c for c in commands
                    if c.startswith('mem8 ') or c.startswith('mem32 '))

    def test_word_aligned_read_uses_mem32_little_endian(self):
        # KUMO (Cortex-M0+): mem8 here would read 0; mem32 returns the CPUID.
        with env():
            result, commands = run_read('ATSAMD21', KUMO_CPUID_BYTES,
                                        address=0xE000ED00, length=4)
        self.assertEqual(result, KUMO_CPUID_BYTES)               # 01 c6 0c 41
        self.assertEqual(int.from_bytes(result, 'little'), 0x410CC601)
        self.assertEqual(self._read_cmd(commands), f'mem32 {hex(0xE000ED00)} 1')

    def test_multiword_read_uses_mem32_with_word_count(self):
        data = bytes(range(16))  # 4 words
        with env():
            result, commands = run_read('ATSAMD21', data,
                                        address=0x20000000, length=16)
        self.assertEqual(result, data)
        self.assertEqual(self._read_cmd(commands), f'mem32 {hex(0x20000000)} 4')

    def test_unaligned_address_falls_back_to_mem8(self):
        data = b'\xAA\xBB\xCC'
        with env():
            result, commands = run_read('ATSAMD21', data,
                                        address=0x20000001, length=3)
        self.assertEqual(result, data)
        self.assertEqual(self._read_cmd(commands), f'mem8 {hex(0x20000001)} 3')

    def test_partial_word_length_falls_back_to_mem8(self):
        data = b'\x01\x02\x03'
        with env():
            result, commands = run_read('ATSAMD21', data,
                                        address=0x20000000, length=3)
        self.assertEqual(result, data)
        self.assertEqual(self._read_cmd(commands), f'mem8 {hex(0x20000000)} 3')

    def test_word_read_composes_with_da1469_reset_halt(self):
        # DA1469x word read must still reset+halt, THEN use mem32.
        with env():
            result, commands = run_read('DA14695', CPUID_BYTES,
                                        address=0xE000ED00, length=4)
        self.assertEqual(result, CPUID_BYTES)
        r_i, h_i = commands.index('r'), commands.index('h')
        mem32_i = next(i for i, c in enumerate(commands) if c.startswith('mem32 '))
        self.assertLess(r_i, h_i)
        self.assertLess(h_i, mem32_i)


class ParseMem32BytesTests(unittest.TestCase):
    def test_single_word_is_little_endian(self):
        out = mem32_text(0xE000ED00, bytes([0x01, 0xC6, 0x0C, 0x41]))
        self.assertEqual(jlink._parse_mem32_bytes(out, 4),
                         bytes([0x01, 0xC6, 0x0C, 0x41]))

    def test_address_column_not_parsed_as_data(self):
        # Left-of-'=' address (also 8 hex digits) must be ignored.
        out = mem32_text(0xE000ED00, bytes([0xDE, 0xAD, 0xBE, 0xEF]))
        self.assertEqual(jlink._parse_mem32_bytes(out, 4),
                         bytes([0xDE, 0xAD, 0xBE, 0xEF]))

    def test_multiple_words_and_length_trim(self):
        data = bytes(range(12))  # 3 words
        out = mem32_text(0x20000000, data)
        self.assertEqual(jlink._parse_mem32_bytes(out, 12), data)
        self.assertEqual(jlink._parse_mem32_bytes(out, 4), data[:4])

    def test_garbled_returns_empty(self):
        self.assertEqual(jlink._parse_mem32_bytes('no equals sign', 4), b'')


if __name__ == '__main__':
    unittest.main()
