# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
J-Link interaction library

This module provides a Python interface for communicating with J-Link
debug probes using the J-Link Commander (JLinkExe).
"""

import logging
import os
import re
import time
from contextlib import closing, contextmanager
import pexpect
from pexpect import replwrap

logger = logging.getLogger(__name__)

# DA1469x external QSPI XIP default: 1 MiB at XIP base (matches common loader erase size).
# Off-chip "offset 0" for the slot maps to CPU XIP 0x16000000 — Commander uses absolute XIP.
# Loader-style "bank 0" targets this window; J-Link uses SetEnableFlashbank(<base>, 1) for that bank.
_DA1469X_QSPI_RANGE_BYTES = 1048576
_DA1469X_QSPI_XIP_START = 0x16000000
_DA1469X_QSPI_FLASH_BANK0_BASE = _DA1469X_QSPI_XIP_START
_DA1469X_QSPI_XIP_END = _DA1469X_QSPI_XIP_START + _DA1469X_QSPI_RANGE_BYTES - 1

# DA1469x QSPI XIP is fetched through the cache controller; the same flash is
# also mapped UNCACHED at XIP + 0x2000_0000 (so 0x16000000 mirrors at
# 0x36000000). Reads via the mirror always hit QSPI, never a stale cache line
# (datasheet memory map: QSPIC cached vs uncached regions).
_DA1469X_QSPI_UNCACHED_OFFSET = 0x20000000
# DA1469x CACHE_CTRL1_REG: writing 1 sets the cache-flush field, so subsequent
# cached fetches refill from QSPI (datasheet: CACHE_CTRL1_REG).
_DA1469X_CACHE_CTRL1_REG = 0x100C0000
# mem8 read-back chunk for the uncached verify: ~256 Commander output lines per
# command — big enough to amortise REPL round-trips, small enough to stay well
# inside pexpect/replwrap buffering.
_UNCACHED_VERIFY_CHUNK = 4096
# J-Link Commander's loadfile compare-failure line; wording varies across
# Commander versions ("Verification failed", "Verify failed").
_VERIFY_FAILED_RE = re.compile(r'verif(?:y|ication)\s+failed', re.IGNORECASE)

# Optional in .JLinkScript: LAGER_ERASE_RANGE: 0x16000000 0x160FFFFF
_LAGER_ERASE_RANGE_PATTERN = re.compile(
    r'LAGER_ERASE_RANGE\s*:\s*(0x[0-9A-Fa-f]+)\s+(0x[0-9A-Fa-f]+)',
    re.IGNORECASE,
)


def parse_lager_erase_range_from_script(script_path):
    """Return (start, end) inclusive from LAGER_ERASE_RANGE in script, or None."""
    if not script_path or not os.path.isfile(script_path):
        return None
    try:
        with open(script_path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError as e:
        logger.debug('Could not read script for LAGER_ERASE_RANGE: %s', e)
        return None
    m = _LAGER_ERASE_RANGE_PATTERN.search(text)
    if not m:
        return None
    try:
        start = int(m.group(1), 16)
        end = int(m.group(2), 16)
    except ValueError:
        return None
    if start > end:
        logger.warning('LAGER_ERASE_RANGE: start > end (%#x > %#x), ignoring', start, end)
        return None
    return (start, end)


def _loadfile_skipped_programming(output):
    """True if J-Link chose not to program because on-device data already matched."""
    if not output:
        return False
    o = output.lower()
    return 'skipped' in o and ('match' in o or 'already' in o)


_LOADFILE_SKIPPED_MSG = (
    'WARNING: J-Link did not write this file (on-device flash already '
    'matched). If you expected a new build, use --erase or fix the path; '
    'after a power cycle the DUT still runs the old image.'
)


def _parse_mem8_bytes(output, length):
    """Parse J-Link Commander ``mem8`` output into bytes.

    Only lines containing '=' carry data ("00000000 = FF FF .."); everything
    else (echoed command, prompt fragments, banners) is skipped. Returns at
    most *length* bytes — fewer means the read came back short or garbled.
    """
    memory_data = []
    for line in output.split('\n'):
        if '=' in line:
            # Split on '=' and take the right side (the memory bytes)
            parts = line.split('=', 1)
            if len(parts) == 2:
                hex_bytes = re.findall(r'([0-9A-Fa-f]{2})', parts[1])
                for hex_byte in hex_bytes:
                    memory_data.append(int(hex_byte, 16))
    return bytes(memory_data[:length])


def _iter_loadfile_cmds(hexfiles, binfiles, elffiles):
    """Yield (command, bin_address_or_None, path) in hex -> bin -> elf order."""
    for file in hexfiles:
        yield f'loadfile {file}', None, file
    for (file, address) in binfiles:
        yield f'loadfile {file} {hex(address)}', address, file
    for file in elffiles:
        yield f'loadfile {file}', None, file


def _loadfile_one(jl, cmd):
    """Run one loadfile; return (output, skip_warning_or_None)."""
    out = jl.run_command(cmd)
    warn = _LOADFILE_SKIPPED_MSG if _loadfile_skipped_programming(out) else None
    return out, warn


def _yield_loadfile_outputs(jl, hexfiles, binfiles, elffiles):
    """Run loadfile for hex, bin, elf lists; yield Commander output and skip warnings."""
    for cmd, _addr, _path in _iter_loadfile_cmds(hexfiles, binfiles, elffiles):
        out, warn = _loadfile_one(jl, cmd)
        yield out
        if warn:
            yield warn


def _verify_bin_uncached(jl, captured_output, path, address):
    """Cache-coherent read-back of one just-programmed QSPI-XIP bin.

    J-Link's loadfile compare reads through the CACHED XIP window; on a
    no-reset attach the cache can hold a stale line at the start of the
    programmed region, so the compare reports a false "Verification failed"
    even though flash is correct. Flush the cache controller, read the image
    back through the uncached mirror on the same Commander session, and
    byte-compare against the file on disk:

    * match        -> yield the captured loadfile output with the (false)
                      compare-failure line(s) removed, then a success note.
    * mismatch     -> yield the captured output unmodified plus a real
                      'Verification failed @ 0x...' line naming the first
                      differing byte (cached-window address).
    * inconclusive -> (short/garbled read, Commander error) yield the captured
                      output unmodified plus a warning; a genuine failure line
                      in the capture survives untouched.

    ``LAGER_DA1469_UNCACHED_VERIFY_BYTES`` caps the compare (0 = whole file).
    Note: with LAGER_DA1469_PRE_FLASH_RUN_HALT=0 the core may be running while
    the flush register is written; the opt-in flag gates that exposure.
    """
    try:
        with open(path, 'rb') as f:
            expected = f.read()
    except OSError as e:
        yield captured_output
        yield f'WARNING: uncached verify skipped - could not read {path} on disk ({e})'
        return

    raw = os.environ.get('LAGER_DA1469_UNCACHED_VERIFY_BYTES', '0').strip()
    try:
        cap = int(raw, 0)
    except ValueError:
        cap = 0
    total = len(expected) if cap <= 0 else min(len(expected), cap)

    mirror = address + _DA1469X_QSPI_UNCACHED_OFFSET
    mismatch = None       # cached-window address of the first differing byte
    inconclusive = None   # human-readable reason
    try:
        # Flush BEFORE any read-back so the cached and uncached views agree.
        jl.run_command(f'w4 {hex(_DA1469X_CACHE_CTRL1_REG)} 1')
        for offset in range(0, total, _UNCACHED_VERIFY_CHUNK):
            n = min(_UNCACHED_VERIFY_CHUNK, total - offset)
            got = _parse_mem8_bytes(jl.run_command(f'mem8 {hex(mirror + offset)} {n}'), n)
            if len(got) != n:
                inconclusive = f'short read at {hex(mirror + offset)} ({len(got)}/{n} bytes)'
                break
            want = expected[offset:offset + n]
            if got != want:
                i = next(i for i, (g, w) in enumerate(zip(got, want)) if g != w)
                mismatch = address + offset + i
                break
    except Exception as e:  # noqa: BLE001 — a dead Commander must not kill the flash stream
        inconclusive = f'{type(e).__name__}: {e}'

    if mismatch is not None:
        yield captured_output
        yield (f'Verification failed @ {hex(mismatch)} '
               f'(uncached QSPI read-back mismatch after cache flush)')
    elif inconclusive is not None:
        yield captured_output
        yield (f'WARNING: uncached verify could not complete ({inconclusive}); '
               f'see loadfile output above')
    else:
        # All compared bytes match flash, so any compare-failure line in the
        # loadfile output was a stale-cache false negative — drop it so
        # consumers grepping for it don't trip. join/split is the identity
        # when no line matches.
        yield '\n'.join(
            line for line in captured_output.split('\n')
            if not _VERIFY_FAILED_RE.search(line)
        )
        yield (f'Uncached read-back OK: {total} of {len(expected)} bytes @ {hex(address)} '
               f'match flash via {hex(mirror)} after cache flush '
               f'(a stale-cache false negative from the J-Link compare, if printed, was dropped)')


def _yield_loadfile_outputs_uncached_verify(jl, hexfiles, binfiles, elffiles):
    """_yield_loadfile_outputs plus an uncached read-back for QSPI-XIP bins.

    hex/elf loads and bins outside the DA1469x cached XIP window stream
    exactly as the default path; an in-window bin gets _verify_bin_uncached's
    verdict between its loadfile output and any skip warning.
    """
    for cmd, addr, path in _iter_loadfile_cmds(hexfiles, binfiles, elffiles):
        out, warn = _loadfile_one(jl, cmd)
        if addr is not None and _DA1469X_QSPI_XIP_START <= addr <= _DA1469X_QSPI_XIP_END:
            yield from _verify_bin_uncached(jl, out, path, addr)
        else:
            yield out
        if warn:
            yield warn


# JLinkExe paths (checked in order)
JLINK_EXE_PATHS = [
    '/tmp/lager-jlink-bin/JLinkExe',  # Symlinks to /opt/SEGGER (most common)
    '/opt/SEGGER/JLink_V794e/JLinkExe',  # Direct path on newer boxes
    '/home/www-data/third_party/jlink/JLinkExe',
    '/home/www-data/third_party/JLink_V884/JLinkExe',
    '/home/www-data/third_party/JLink_Linux_V794a_x86_64/JLinkExe',
    '/usr/bin/JLinkExe',
]


def get_jlink_exe_path():
    """Find JLinkExe executable, return None if not found."""
    for path in JLINK_EXE_PATHS:
        if os.path.exists(path):
            return path
    return None


def _serial_from_gdbserver_cmdline(cmdline):
    """Extract the J-Link USB serial from a JLinkGDBServer cmdline list.

    JLinkGDBServer uses ``-select USB=<sn>`` (or bare ``-select USB`` for any
    probe). Returns the serial string, or None when no serial is bound.
    """
    try:
        idx = cmdline.index('-select')
    except ValueError:
        return None
    if idx + 1 >= len(cmdline):
        return None
    value = cmdline[idx + 1]
    if not value or not value.startswith('USB='):
        return None
    serial = value[len('USB='):].strip()
    return serial or None


@contextmanager
def commander(args, script_file=None, serial=None):
    """
    Context manager for J-Link Commander REPL wrapper

    Args:
        args: Command-line arguments for JLinkExe
        script_file: Optional path to J-Link script file (.JLinkScript)
        serial: J-Link USB serial. When provided, JLinkExe is bound to that
            specific probe via ``-SelectEmuBySN <sn>`` so the call doesn't
            collide with another probe on the same box.

    Yields:
        REPLWrapper instance for interacting with J-Link Commander

    Example:
        with commander(['-device', 'NRF52840_XXAA']) as jl:
            output = jl.run_command('connect')

        # With script file:
        with commander(['-device', 'NRF52840_XXAA'], script_file='/tmp/init.JLinkScript') as jl:
            output = jl.run_command('connect')
    """
    jlink_exe = get_jlink_exe_path()
    if not jlink_exe:
        raise Exception('JLinkExe not found')

    full_args = list(args)
    if serial and '-SelectEmuBySN' not in full_args:
        full_args = ['-SelectEmuBySN', serial] + full_args
    if script_file and os.path.exists(script_file):
        full_args.extend(['-JLinkScriptFile', script_file])
    elif script_file:
        logger.warning('JLink commander: script_file %r missing on disk; continuing without', script_file)

    # use_poll=True so REPLWrapper's expect loop uses poll() instead of select().
    # The debug service is long-lived; once it holds >= 1024 open fds, JLinkExe's
    # child PTY lands at an fd >= FD_SETSIZE (1024) and select() raises
    # "ValueError: filedescriptor out of range in select()" — which surfaces as a
    # 500 on /debug/erase and /debug/flash. poll() has no FD_SETSIZE ceiling.
    child = pexpect.spawn(jlink_exe, full_args, encoding='utf-8', use_poll=True)
    with closing(child):
        try:
            repl = replwrap.REPLWrapper(child, "J-Link>", None)
            yield repl
            repl.run_command('q')
        except pexpect.exceptions.EOF:
            pass


class JLink:
    """
    Class for communicating with J-Link debug probes
    """

    def __init__(self, cmdline, script_file=None, serial=None):
        """
        Initialize J-Link interface from command line

        Args:
            cmdline: Command line arguments list from running JLinkGDBServer process
            script_file: Optional path to J-Link script file (.JLinkScript)
            serial: J-Link USB serial. If None, attempts to recover it from
                ``-select USB=<sn>`` in *cmdline* so the spawned JLinkExe binds
                to the same physical probe.
        """
        args_start = cmdline.index('-device')
        args = cmdline[args_start:]
        if not args[-1]:
            args = args[:-1]
        speed_idx = args.index('-speed')
        args = args[:speed_idx + 2]
        self.args = args
        self.script_file = script_file
        if serial is None:
            serial = _serial_from_gdbserver_cmdline(cmdline)
        self.serial = serial

    def erase(self, start_addr, length, *, close=True):
        """
        Erase flash memory

        Args:
            start_addr: Starting address for erase
            length: Number of bytes to erase
            close: Whether to close connection after operation (unused for J-Link)

        Yields:
            Output from J-Link commands
        """
        with commander(self.args, script_file=self.script_file, serial=self.serial) as jl:
            yield jl.run_command('connect')
            yield jl.run_command(f'erase {hex(start_addr)} {hex(start_addr + length - 1)}')

    def chip_erase(self, *, close=True):
        """
        Perform chip erase (non-DA1469) or **address-range erase** on DA1469x external QSPI.

        SEGGER Commander supports ``erase <SAddr> <EAddr>`` for a range only (see J-Link
        Commander docs). For **DA1469x** we use that instead of a global ``erase``:
        full chip erase wipes internal + external and can interact badly with the next
        ``loadfile``; slot-style erase over the XIP map (default 1 MiB @ 0x16000000, or
        ``LAGER_ERASE_RANGE`` in the J-Link script) is closer to typical loader behavior.
        Sequence: ``Exec SetEnableFlashbank``, ``Exec EnableEraseAllFlashBanks``,
        ``erase <start> <end>`` — then disconnect (no extra Commander reset hooks).

        Other devices: plain ``erase`` (whole chip).

        Args:
            close: Whether to close connection after operation (unused for J-Link)

        Yields:
            Output from J-Link commands
        """
        with commander(self.args, script_file=self.script_file, serial=self.serial) as jl:
            yield jl.run_command('connect')
            dev = ''
            try:
                di = self.args.index('-device')
                dev = self.args[di + 1]
            except (ValueError, IndexError):
                pass
            is_da1469 = 'DA1469' in (dev or '').upper()
            if is_da1469:
                # Address-range erase only — never Commander ``erase`` without addresses
                # (that would be full chip erase on this device family).
                logger.info('DA1469x: address-range erase only (no full chip erase)')
                # External QSPI flash bank for XIP 0x16000000 (loader "bank 0"); not internal flash.
                yield jl.run_command(
                    f'Exec SetEnableFlashbank {hex(_DA1469X_QSPI_FLASH_BANK0_BASE)}=1'
                )
                yield jl.run_command('Exec EnableEraseAllFlashBanks')
                qspi_start, qspi_end = _DA1469X_QSPI_XIP_START, _DA1469X_QSPI_XIP_END
                parsed = parse_lager_erase_range_from_script(self.script_file)
                if parsed:
                    qspi_start, qspi_end = parsed
                logger.info(
                    'DA1469x: QSPI bank0 base %#x; range erase %s–%s',
                    _DA1469X_QSPI_FLASH_BANK0_BASE,
                    hex(qspi_start),
                    hex(qspi_end),
                )
                yield jl.run_command(
                    f'erase {hex(qspi_start)} {hex(qspi_end)}'
                )
            else:
                yield jl.run_command('erase')

    def read_memory(self, address, length, *, close=True):
        """
        Read memory from device

        Args:
            address: Starting memory address (int)
            length: Number of bytes to read
            close: Whether to close connection after operation (unused for J-Link)

        Returns:
            bytes object containing the memory data
        """
        with commander(self.args, script_file=self.script_file, serial=self.serial) as jl:
            jl.run_command('connect')
            # J-Link Commander mem8 syntax: mem8 address count
            output = jl.run_command(f'mem8 {hex(address)} {length}')
        return _parse_mem8_bytes(output, length)

    def flash(self, files, preverify=False, verify=False, *, close=True):
        """
        Flash firmware to device

        Args:
            files: Tuple of (hexfiles, binfiles, elffiles) to program
            preverify: Whether to verify before flashing (unused for J-Link)
            verify: Whether to verify after flashing (unused for J-Link)
            close: Whether to close connection after operation (unused for J-Link)

        Yields:
            Output from J-Link commands, plus a WARNING line if loadfile was skipped
            because flash already matched (no bytes written).

        **DA1469x:** ``rnh`` / ``h`` before ``loadfile`` (``LAGER_DA1469_PRE_FLASH_RUN_HALT=0`` to
        skip). Set ``LAGER_DA1469_UNCACHED_VERIFY=1`` (default off) for a cache-coherent
        read-back of QSPI-XIP bins through the uncached mirror after each ``loadfile`` —
        suppresses stale-cache false verify failures, reports real mismatches;
        ``LAGER_DA1469_UNCACHED_VERIFY_BYTES`` caps the compare (0 = whole file).
        """
        (hexfiles, binfiles, elffiles) = files
        with commander(self.args, script_file=self.script_file, serial=self.serial) as jl:
            # Yield connect output to show device discovery details
            yield jl.run_command('connect')

            # DA1469x: after erase, programming from a cold halted attach can fail even though
            # QSPI itself still reads erased data. Run briefly, then halt before loadfile so the
            # first flash starts from a known-good controller/boot state.
            dev = ''
            try:
                di = self.args.index('-device')
                dev = self.args[di + 1]
            except (ValueError, IndexError):
                pass
            if 'DA1469' in (dev or '').upper():
                pre = os.environ.get('LAGER_DA1469_PRE_FLASH_RUN_HALT', '1').strip().lower()
                if pre not in ('0', 'false', 'no', 'off'):
                    logger.info('DA1469x: rnh, settle, h before loadfile')
                    yield jl.run_command('rnh')
                    time.sleep(0.1)
                    yield jl.run_command('h')
                # Opt-in cache-coherent post-program verify (default OFF; an
                # empty value counts as off, unlike the default-on flag above).
                uncached = os.environ.get('LAGER_DA1469_UNCACHED_VERIFY', '0').strip().lower()
                if uncached and uncached not in ('0', 'false', 'no', 'off'):
                    logger.info('DA1469x: uncached read-back verify after loadfile')
                    yield from _yield_loadfile_outputs_uncached_verify(
                        jl, hexfiles, binfiles, elffiles)
                    return

            yield from _yield_loadfile_outputs(jl, hexfiles, binfiles, elffiles)

    def reset(self, halt, *, close=True):
        """
        Reset the device

        Args:
            halt: Whether to halt after reset
            close: Whether to close connection after operation (unused for J-Link)

        Yields:
            Output from J-Link commands
        """
        with commander(self.args, script_file=self.script_file, serial=self.serial) as jl:
            yield jl.run_command('connect')
            if halt:
                yield jl.run_command('r')
                yield jl.run_command('h')
            else:
                # rnh == reset no halt
                yield jl.run_command('rnh')

    def run(self, *, close=True):
        """
        Run the device (reset without halt)

        Args:
            close: Whether to close connection after operation (unused for J-Link)

        Yields:
            Output from J-Link commands
        """
        yield from self.reset(halt=False)