# Copyright 2024-2026 Lager Data LLC
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


def _yield_loadfile_outputs(jl, hexfiles, binfiles, elffiles):
    """Run loadfile for hex, bin, elf lists; yield Commander output and skip warnings."""
    for file in hexfiles:
        out = jl.run_command(f'loadfile {file}')
        yield out
        if _loadfile_skipped_programming(out):
            yield _LOADFILE_SKIPPED_MSG
    for (file, address) in binfiles:
        out = jl.run_command(f'loadfile {file} {hex(address)}')
        yield out
        if _loadfile_skipped_programming(out):
            yield _LOADFILE_SKIPPED_MSG
    for file in elffiles:
        out = jl.run_command(f'loadfile {file}')
        yield out
        if _loadfile_skipped_programming(out):
            yield _LOADFILE_SKIPPED_MSG


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


@contextmanager
def commander(args, script_file=None):
    """
    Context manager for J-Link Commander REPL wrapper

    Args:
        args: Command-line arguments for JLinkExe
        script_file: Optional path to J-Link script file (.JLinkScript)

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
    if script_file and os.path.exists(script_file):
        full_args.extend(['-JLinkScriptFile', script_file])
    elif script_file:
        logger.warning('JLink commander: script_file %r missing on disk; continuing without', script_file)

    child = pexpect.spawn(jlink_exe, full_args, encoding='utf-8')
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

    def __init__(self, cmdline, script_file=None):
        """
        Initialize J-Link interface from command line

        Args:
            cmdline: Command line arguments list from running process
            script_file: Optional path to J-Link script file (.JLinkScript)
        """
        args_start = cmdline.index('-device')
        args = cmdline[args_start:]
        if not args[-1]:
            args = args[:-1]
        speed_idx = args.index('-speed')
        args = args[:speed_idx + 2]
        self.args = args
        self.script_file = script_file
        self.device = ''
        try:
            device_idx = self.args.index('-device')
            self.device = self.args[device_idx + 1]
        except (ValueError, IndexError):
            pass

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
        with commander(self.args, script_file=self.script_file) as jl:
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
        with commander(self.args, script_file=self.script_file) as jl:
            yield jl.run_command('connect')
            is_da1469 = 'DA1469' in (self.device or '').upper()
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
        memory_data = []

        with commander(self.args, script_file=self.script_file) as jl:
            jl.run_command('connect')
            # J-Link Commander mem8 syntax: mem8 address count
            output = jl.run_command(f'mem8 {hex(address)} {length}')

            # Parse output format: "00000000 = FF FF FF FF ..."
            # Each line contains one address worth of data
            for line in output.split('\n'):
                if '=' in line:
                    # Split on '=' and take the right side (the memory bytes)
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        data_part = parts[1]
                        # Extract hex bytes (2-character hex patterns)
                        hex_bytes = re.findall(r'([0-9A-Fa-f]{2})', data_part)
                        for hex_byte in hex_bytes:
                            memory_data.append(int(hex_byte, 16))

        # Trim to requested length
        return bytes(memory_data[:length])

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
        skip).
        """
        (hexfiles, binfiles, elffiles) = files
        with commander(self.args, script_file=self.script_file) as jl:
            # Yield connect output to show device discovery details
            yield jl.run_command('connect')

            # DA1469x: after erase, programming from a cold halted attach can fail even though
            # QSPI itself still reads erased data. Run briefly, then halt before loadfile so the
            # first flash starts from a known-good controller/boot state.
            if 'DA1469' in (self.device or '').upper():
                pre = os.environ.get('LAGER_DA1469_PRE_FLASH_RUN_HALT', '1').strip().lower()
                if pre not in ('0', 'false', 'no', 'off'):
                    logger.info('DA1469x: rnh, settle, h before loadfile')
                    yield jl.run_command('rnh')
                    time.sleep(0.1)
                    yield jl.run_command('h')

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
        with commander(self.args, script_file=self.script_file) as jl:
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