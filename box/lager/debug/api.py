# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Unified debug API - J-Link Only

This module provides high-level functions for debug operations including
connect, disconnect, reset, flash, and erase operations.
"""

import os
import json
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path
from .jlink import JLink, commander
from . import jlink as _jlink_mod
from .mappings import (
    get_jlink_status,
    readfile,
    JL_LOGFILE,
)
from .process import (
    stop_jlink,
)
from .gdbserver import get_jlink_gdbserver_status, stop_jlink_gdbserver, start_jlink_gdbserver
from .gdb import get_arch, reset as gdb_reset, read_memory as gdb_read_memory
from .probe_lock import holds_probe
from . import probes as _probes
from .probes import (
    gdb_port_for_slot,
    rtt_port_for_slot,
    jlink_gdbserver_logfile,
)

logger = logging.getLogger(__name__)

# Legacy shared path. Kept ONLY so an upgrade can clean up a file an older
# build left behind (see purge_legacy_script_file); nothing writes here now.
#
# It used to be the one path every debug operation read from, which is the bug
# fixed here: an operation that never asked for a script silently inherited
# whichever script was written last -- by a different net, a different session,
# or a test suite that had since finished. Combined with an erased target that
# made the debugger unable to attach, so one scripted flash could take a bench
# out of service until somebody deleted this file by hand.
JLINK_SCRIPT_TEMP_PATH = '/tmp/lager_jlink_script.JLinkScript'

# The per-net script and cfg basenames. Their root is ``probes.RUNTIME_DIR``,
# read through the module at call time so redirecting it in one place
# redirects every builder and every containment check that names it -- the
# debug layer spans four modules and probes is the only one all of them can
# import.
SCRIPT_NAME_TEMPLATE = 'lager_jlink_script_{}.JLinkScript'

# Per-connect OpenOCD cfg overrides. NOT the same file as
# ``OPENOCD_CONFIG_TEMP_PATH`` in service.py: that one is the box-wide cfg
# the HTTP debug service and the net record both write, and it stays shared.
# This one is scoped to one net for one session, so an in-process override
# cannot leak onto every other net on the box (issue #195, in its OpenOCD
# form).
CONFIG_NAME_TEMPLATE = 'lager_openocd_cfg_{}.cfg'


def _net_slug(net_name):
    """Filesystem-safe form of a net name, or None.

    Net names come from user config, so they are not safe to interpolate into a
    path unchecked. Anything outside [A-Za-z0-9._-] becomes '_'.
    """
    if not net_name or not str(net_name).strip():
        return None
    return re.sub(r'[^A-Za-z0-9._-]', '_', str(net_name).strip())


def script_path_for_net(net_name):
    """Where this net's J-Link script lives, or None if the net is unknown.

    Per net, deliberately. A script describes how to attach to one target; it is
    not a property of the box.

    The slug is the real defence; the containment check states where the result
    is allowed to land, next to the join that produces it. See
    lager.util.paths for why that check is not shared.
    """
    slug = _net_slug(net_name)
    if not slug:
        return None
    path = os.path.normpath(
        os.path.join(_probes.RUNTIME_DIR, SCRIPT_NAME_TEMPLATE.format(slug)))
    if not path.startswith(_probes.RUNTIME_DIR + os.sep):
        raise ValueError(f'refusing a path outside {_probes.RUNTIME_DIR!r}')
    return path


def config_path_for_net(net_name):
    """Where this net's per-connect OpenOCD cfg lives, or None.

    The OpenOCD counterpart of :func:`script_path_for_net`, and per net for
    the same reason: a cfg describes how to attach to one target, not a
    property of the box.
    """
    slug = _net_slug(net_name)
    if not slug:
        return None
    path = os.path.normpath(
        os.path.join(_probes.RUNTIME_DIR, CONFIG_NAME_TEMPLATE.format(slug)))
    if not path.startswith(_probes.RUNTIME_DIR + os.sep):
        raise ValueError(f'refusing a path outside {_probes.RUNTIME_DIR!r}')
    return path


def clear_config_file(net_name):
    """Remove this net's per-connect OpenOCD cfg. Called when its session ends.

    The OpenOCD counterpart of :func:`clear_script_file`, and load-bearing for
    the same reason: without it the override outlives the session that set it.
    """
    # Joined inline rather than through the builder above: a containment check
    # is only credited to the function that performs the join, so calling the
    # builder here would leave this correct and the analysis blind. See
    # lager.util.paths. The slug is the real defence.
    slug = _net_slug(net_name)
    if not slug:
        return False
    path = os.path.normpath(
        os.path.join(_probes.RUNTIME_DIR, CONFIG_NAME_TEMPLATE.format(slug)))
    if not path.startswith(_probes.RUNTIME_DIR + os.sep):
        raise ValueError(f'refusing a path outside {_probes.RUNTIME_DIR!r}')
    try:
        os.remove(path)
        logger.info('Cleared OpenOCD cfg for net %s (%s)', net_name, path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning('Could not clear OpenOCD cfg %s: %s', path, e)
        return False


def _get_script_file(net_name=None):
    """Return this net's script path if it exists, else None.

    Returns None when *net_name* is None. There is deliberately no fall back to
    "whatever script happens to be on disk" -- an operation that did not ask for
    a script must not silently get one. Callers that legitimately have a script
    pass it down explicitly as ``script_file=``.
    """
    path = script_path_for_net(net_name)
    if path and os.path.exists(path):
        return path
    return None


def clear_script_file(net_name):
    """Remove this net's script. Called when its debug session ends.

    Without this a script outlives the session that established it, so the next
    operation on the net -- possibly days later, possibly from a different
    caller -- silently runs under it.
    """
    # Joined inline rather than through the builder above: a containment check
    # is only credited to the function that performs the join, so calling the
    # builder here would leave this correct and the analysis blind. See
    # lager.util.paths. The slug is the real defence.
    slug = _net_slug(net_name)
    if not slug:
        return False
    path = os.path.normpath(
        os.path.join(_probes.RUNTIME_DIR, SCRIPT_NAME_TEMPLATE.format(slug)))
    if not path.startswith(_probes.RUNTIME_DIR + os.sep):
        raise ValueError(f'refusing a path outside {_probes.RUNTIME_DIR!r}')
    try:
        os.remove(path)
        logger.info('Cleared J-Link script for net %s (%s)', net_name, path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning('Could not clear J-Link script %s: %s', path, e)
        return False


def purge_legacy_script_file():
    """Delete the pre-per-net shared script if an older build left one.

    A box upgrading into this fix can still be carrying the poisoned file, and
    nothing reads it any more -- so it would sit there confusing whoever next
    goes looking. Best effort.
    """
    try:
        os.remove(JLINK_SCRIPT_TEMP_PATH)
        logger.info('Removed legacy shared J-Link script %s', JLINK_SCRIPT_TEMP_PATH)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# J-Link Commander does not raise when it cannot attach -- it prints and carries
# on, so the only way to notice is to read its output. These are the lines it
# produces for a target it could not reach.
_CONNECT_FAILED_RE = re.compile(
    r'could not connect to (?:the )?target'
    r'|cannot connect to target'
    r'|failed to power up dap'
    r'|could not read cpuid',
    re.IGNORECASE,
)


def _connect_failed(output_chunks):
    """True if Commander output shows it never attached."""
    return bool(_CONNECT_FAILED_RE.search('\n'.join(output_chunks)))


# The subset of the above that is safe to FAIL an operation on, rather than
# merely to retry it.
#
# `could not read cpuid` is deliberately absent. J-Link emits it per access
# port -- `AP[0]: Skipped. Could not read CPUID register` -- while scanning,
# so on its own it does not establish that the session never attached. As a
# retry trigger that costs one extra attempt; as a verdict it would report a
# completed operation as failed. Nothing is lost by excluding it: in the
# captured failures it always appears alongside `Could not connect to target.`
# (see test/unit/box/test_jlink_script_attach_retry.py), which is matched here.
#
# The last four are Commander failing to use the probe at all, seen when a
# second J-Link client was driving the same probe: every later command is
# refused, the session exits normally, and an erase or flash that touched
# nothing looked complete. Kept in step with `_PROBE_UNUSABLE_SIGNATURES` in
# cli/commands/development/debug/commands.py.
_ATTACH_FAILED_RE = re.compile(
    r'could not connect to (?:the )?target'
    r'|cannot connect to target'
    r'|failed to power up dap'
    r'|selected interface \(\w+\) is not supported by the connected probe'
    r'|target connection not established yet but required for command'
    r'|j-link connection not established yet but required for command'
    r'|connecting to j-link via usb\.\.\.failed',
    re.IGNORECASE,
)


def _attach_failed(output_chunks):
    """True if Commander output shows it never attached, strictly enough to
    fail the operation on rather than retry it."""
    return bool(_ATTACH_FAILED_RE.search('\n'.join(output_chunks)))


# Lines J-Link Commander prints when `loadfile` programmed nothing: its flash
# RAMCode could not be downloaded or set up. `Downloading file [...]` comes
# BEFORE these, so it is no evidence of programming on its own.
#
# Kept in step with `_FLASH_PROGRAMMING_FAILURE_LINES` / `_PREFIXES` in
# cli/commands/development/debug/commands.py: the box and the CLI must not
# disagree about the same output. Matched per line, after J-Link's
# `****** Error: ` banner, as the whole line or its start -- never a substring.
# `Failed to download RAMCode` is exact-only because J-Link also prints
# `... for indirect memory access!` and FPU variants, which are not flash
# programming. Verify failures are deliberately absent: on a DA1469x the
# cached-XIP compare reports a false one on a correctly programmed part.
_PROGRAMMING_FAILED_RE = re.compile(
    r'^[*\s]*(?:error:\s*)?'
    r'(?:failed to download ramcode[!.]'
    r'|failed to prepare for programming\.'
    r'|error while programming flash: programming failed\.'
    r'|verification of ramcode failed.*'
    r'|error while determining flash info.*)\s*$',
    re.IGNORECASE,
)


# J-Link's evidence that `loadfile` reached flash: one line per bank, including
# `Skipped. Contents already match` when there was nothing to write, or a bare
# `O.K.` for a load that touched no flash bank. Checked per `loadfile`: each
# `Downloading file` needs one before the next.
_FLASH_DOWNLOAD_RE = re.compile(r'^\s*(?:J-Link:\s*)?Flash download:', re.IGNORECASE)
_DOWNLOADING_FILE_RE = re.compile(r'^\s*Downloading file\b', re.IGNORECASE)

# `jlink.COMMANDER_EXITED`, spelt out: api.py is also loaded against a stubbed
# `.jlink`. test_flash_programming_verdict pins the two together.
_COMMANDER_EXITED = 'JLinkExe exited'

NO_LOADFILE = ('J-Link printed no `Downloading file` line: `loadfile` never ran, '
               'so nothing was programmed')
NO_FLASH_DOWNLOAD = ('J-Link printed `Downloading file` but no `Flash download` '
                     'line after it: nothing was programmed')


def _is_flash_evidence(line):
    return bool(_FLASH_DOWNLOAD_RE.search(line)) or line.strip() == 'O.K.'


def _flash_failure(output_chunks):
    """The line showing a J-Link flash session programmed nothing, else None.

    Failure lines win: JLinkExe exiting under us, a programming failure, a
    failed attach or an unusable probe. Short of those, success needs
    evidence -- a `Downloading file` for the load, and a `Flash download` line
    (or `O.K.`) after each one. A session with no evidence programmed
    nothing, however quiet it was: a J-Link that drops off USB mid-session
    can leave no text at all. Pass only the flash session's own Commander
    output: a connect error from the post-flash reconnect says nothing about
    the flash.
    """
    lines = '\n'.join(output_chunks).splitlines()
    for line in lines:
        if line.strip().startswith(_COMMANDER_EXITED):
            return line.strip()
    for pattern in (_PROGRAMMING_FAILED_RE, _ATTACH_FAILED_RE):
        for line in lines:
            if pattern.search(line):
                return line.strip()
    downloading = seen = False
    for line in lines:
        if _DOWNLOADING_FILE_RE.search(line):
            if downloading:
                return NO_FLASH_DOWNLOAD
            downloading = seen = True
        elif downloading and _is_flash_evidence(line):
            downloading = False
    if downloading:
        return NO_FLASH_DOWNLOAD
    return None if seen else NO_LOADFILE


# J-Link's erase confirmations: `Erasing done.` after a chip or range erase,
# `Flash sectors within Range [...] deleted.` for a range.
_ERASE_DONE_RE = re.compile(
    r'^\s*(?:Erasing done\.|Mass erase done\.|Flash sectors within Range .* deleted\.)',
    re.IGNORECASE | re.MULTILINE,
)
NO_ERASE_DONE = 'J-Link printed no `Erasing done.` line'


def _erase_failure(output_chunks):
    """The line showing a J-Link erase touched nothing, else None.

    A failed attach or an unusable probe names itself; short of that, an
    erase with no J-Link confirmation erased nothing.
    """
    joined = '\n'.join(output_chunks)
    for line in joined.splitlines():
        if _ATTACH_FAILED_RE.search(line):
            return line.strip()
    return None if _ERASE_DONE_RE.search(joined) else NO_ERASE_DONE


# The two causes of a failed RAMCode download seen so far, told apart after
# the fact: a second J-Link client driving the same probe, or the target
# resetting mid-download (a DA1469x SYS watchdog reads back all zeros).
# Neither is visible in J-Link's own output, so a failed flash reports both.

_DA1469X_RESET_CAUSES = (
    (0, 'power-on'),
    (1, 'nRESET pin'),
    (2, 'software'),
    (3, 'SYS watchdog'),
    (4, 'SWD hardware reset'),
    (5, 'CMAC watchdog'),
)

_MEM32_RE = re.compile(r'^\s*[0-9A-Fa-f]{8}\s*=\s*([0-9A-Fa-f]{8})\b', re.MULTILINE)

# How a J-Link client names its probe on the command line: `-SelectEmuBySN
# <sn>` (Commander), `-select USB=<sn>` (GDB server), `-USB <sn>`. SEGGER
# prints and accepts serials without leading zeros, so they compare as numbers.
_SERIAL_SELECTOR_RE = re.compile(r'(?:selectemubysn|usb)\s*[=\s]\s*(\d+)', re.IGNORECASE)


def _same_serial(a, b):
    try:
        return int(str(a)) == int(str(b))
    except ValueError:
        return str(a) == str(b)


def _jlink_processes(serial, proc_root='/proc', exclude_ppid=None):
    """J-Link processes on this box that may be using probe *serial*.

    Read from /proc, so it finds clients lager did not start and does not
    track. A process that selects a probe by serial counts only if that serial
    is *serial* (compared as numbers); one that names no probe takes whichever
    it finds first, so it counts too. Children of *exclude_ppid* (this
    process's own JLinkExe) are left out. Returns ``'<pid> <command line>'``.
    """
    found = []
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, 'cmdline'), 'rb') as f:
                argv = [a.decode(errors='replace') for a in f.read().split(b'\0') if a]
        except OSError:
            continue
        if not argv or not os.path.basename(argv[0]).startswith('JLink'):
            continue
        if exclude_ppid is not None and _ppid(proc_root, pid) == exclude_ppid:
            continue
        cmdline = ' '.join(argv)
        selected = _SERIAL_SELECTOR_RE.findall(cmdline)
        if serial and selected and not any(_same_serial(s, serial) for s in selected):
            continue
        found.append(f'{pid} {cmdline}' + ('' if selected else '  (names no probe)'))
    return found


def _ppid(proc_root, pid):
    try:
        with open(os.path.join(proc_root, pid, 'stat'), encoding='utf-8') as f:
            # `pid (comm) state ppid ...`; comm may hold spaces, so split after it.
            return int(f.read().rsplit(')', 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


class _JLinkClientSampler:
    """Watch for other J-Link clients on a probe while a flash runs.

    A client that interferes and exits before the flash fails is gone by the
    time the failure is diagnosed, so the scan runs every *interval* seconds
    for the whole Commander session and keeps everything it saw. This
    process's own JLinkExe is excluded by parent pid.
    """

    def __init__(self, serial, interval=0.25):
        self.serial = serial
        self.interval = interval
        self.seen = {}
        self._stop = None
        self._thread = None

    def _scan(self):
        for entry in _jlink_processes(self.serial, exclude_ppid=os.getpid()):
            self.seen.setdefault(entry.split(' ', 1)[0], entry)

    def _run(self):
        while not self._stop.wait(self.interval):
            self._scan()

    def __enter__(self):
        import threading
        self._stop = threading.Event()
        self._scan()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2)
        self._scan()
        return False


def _da1469x_reset_causes(jlink_args, script_file, serial, attempts=3, wait_s=1.5):
    """(RESET_STAT_REG value, cause names), or (None, reason) if unreadable.

    Tried *attempts* times, *wait_s* apart: when another client is on the
    probe this read fails for the same reason the flash did, and the other
    client may be gone a moment later.
    """
    reason = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(wait_s)
        try:
            with commander(jlink_args, script_file=script_file, serial=serial) as jl:
                jl.run_command('connect')
                output = jl.run_command(f'mem32 {hex(_jlink_mod.DA1469X_RESET_STAT_REG)} 1')
        except Exception as exc:  # noqa: BLE001 -- a diagnosis must not mask the failure
            reason = f'{type(exc).__name__}: {exc}'
            continue
        match = _MEM32_RE.search(str(output))
        if not match:
            reason = 'no value in the Commander output'
            continue
        value = int(match.group(1), 16)
        return value, [name for bit, name in _DA1469X_RESET_CAUSES if value & (1 << bit)]
    return None, f'{reason}; tried {attempts} times'


def _flash_failure_diagnosis(device, jlink_args, script_file, serial, seen=()):
    """Lines saying what else was going on when a J-Link flash programmed nothing.

    *seen* is what a `_JLinkClientSampler` saw during the flash; the probe is
    scanned again now and the two are reported together.
    """
    lines = []
    others = dict((entry.split(' ', 1)[0], entry) for entry in seen)
    for entry in _jlink_processes(serial, exclude_ppid=os.getpid()):
        others.setdefault(entry.split(' ', 1)[0], entry)
    if others:
        lines.append('Diagnosis: another J-Link client used this probe during the flash, '
                     'which can stop Commander using it or corrupt the RAMCode download. '
                     'Stop it and flash again:')
        lines.extend(f'  {proc}' for proc in others.values())
    else:
        lines.append('Diagnosis: no other J-Link client was seen on this probe.')
    if _probes.is_da1469x(device):
        value, causes = _da1469x_reset_causes(jlink_args, script_file, serial)
        if value is None:
            lines.append(f'Diagnosis: could not read RESET_STAT_REG ({causes}).')
        elif causes:
            lines.append(f'Diagnosis: the target reset during programming '
                         f'(RESET_STAT_REG=0x{value:08X}: {", ".join(causes)}).')
        else:
            lines.append('Diagnosis: the target did not reset during programming '
                         '(RESET_STAT_REG=0x00000000).')
    return lines


class DebugError(Exception):
    """Base class for debug errors"""
    pass


class JLinkStartError(DebugError):
    """Error starting J-Link"""
    def __init__(self, stdout, stderr, logfile):
        self.stdout = stdout
        self.stderr = stderr
        self.logfile = logfile
        # Decode stderr if it's bytes
        if isinstance(stderr, bytes):
            stderr_str = stderr.decode('utf-8', errors='replace')
        else:
            stderr_str = str(stderr)
        super().__init__(stderr_str)


class JLinkAlreadyRunningError(DebugError):
    """J-Link is already running"""
    pass


class JLinkNotRunning(DebugError):
    """J-Link is not running"""
    pass


def ensure_int(value):
    """Ensure value is a valid integer"""
    try:
        return int(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid integer value: {value}")


def validate_speed(speed):
    """
    Validate speed parameter

    Args:
        speed: Speed value as string (kHz) or 'adaptive'

    Returns:
        Validated speed string

    Raises:
        ValueError: If speed is invalid
    """
    if speed == 'adaptive':
        return speed

    try:
        speed_int = int(speed)
    except (ValueError, TypeError):
        raise ValueError(
            f"Invalid speed value: '{speed}'. "
            f"Speed must be a positive integer (in kHz) or 'adaptive'"
        )

    if speed_int <= 0:
        raise ValueError(
            f"Invalid speed: {speed_int} kHz. "
            f"Speed must be a positive integer greater than 0"
        )

    if speed_int > 50000:  # 50 MHz is unrealistically high for SWD/JTAG
        raise ValueError(
            f"Invalid speed: {speed_int} kHz. "
            f"Maximum supported speed is 50000 kHz (50 MHz). "
            f"Typical speeds: 100-4000 kHz"
        )

    # Return a normalized *string*. Callers may pass an int; the retry ladder and
    # the gdbserver argv are built from string literals, so returning the original
    # object unchanged lets an int leak through and make the ladder mixed-type —
    # which raises "sequence item 0: expected str instance, int found" while
    # formatting the connection-failure message.
    return str(speed_int)


def clean_logfile_content(logfile_content, max_length=2000):
    """
    Clean logfile content for error messages

    Args:
        logfile_content: Raw logfile content (may contain null bytes)
        max_length: Maximum length to return

    Returns:
        Cleaned logfile content string
    """
    if logfile_content is None:
        return ''
    if isinstance(logfile_content, bytes):
        # Remove null bytes and decode
        cleaned = logfile_content.replace(b'\x00', b'').decode('utf-8', errors='ignore')
    else:
        # Remove null bytes from string
        cleaned = logfile_content.replace('\x00', '')

    # Limit length and add truncation notice if needed
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length] + f"\n\n... (truncated, total length: {len(logfile_content)} bytes)"

    return cleaned.strip()


def detect_and_configure_rtt(device_type=None, search_addr=0x20000000, search_size=0x10000,
                             chunk_size=0x1000, serial=None, gdb_port=2331):
    """
    Detect RTT control block in RAM and configure J-Link to use it.

    This function searches RAM for the RTT control block signature ("SEGGER RTT")
    and tells J-Link where to find it. This is necessary because:

    1. J-Link only searches for RTT at 4KB-aligned addresses by default
    2. Many firmwares (especially Rust) place RTT at non-aligned addresses
    3. Without this, RTT streaming will fail even though firmware has RTT enabled

    Args:
        device_type: Optional device type hint (not currently used)
        search_addr: RAM start address to search (default: 0x20000000)
        search_size: Size of RAM region to search in bytes (default: 0x10000 / 64KB)
        chunk_size: Size of each read chunk in bytes (default: 0x1000 / 4KB)
        serial: J-Link USB serial. None reads the legacy single-probe state.
        gdb_port: GDB server port the running J-Link is listening on.

    Returns:
        dict with 'found': bool, 'address': str (hex) if found, 'error': str if error
    """
    from .gdb import get_controller

    result = {
        'found': False,
        'address': None,
        'error': None
    }

    try:
        # Check if debugger is connected (check both PID file paths)
        jlink_status = get_jlink_status(serial=serial, gdb_port=gdb_port)
        gdbserver_status = get_jlink_gdbserver_status(serial=serial)
        if not jlink_status['running'] and not gdbserver_status['running']:
            result['error'] = 'No debugger connection'
            return result

        # Get GDB controller
        gdbmi = get_controller(device=device_type, port=gdb_port)

        logger.info('Searching RAM for RTT control block...')

        # RTT control block starts with magic bytes: "SEGGER RTT"
        # Search common RAM regions (typically 0x20000000 - 0x20010000 for most ARM devices)
        rtt_signature = b'SEGGER RTT'
        rtt_address = None

        for offset in range(0, search_size, chunk_size):
            addr = search_addr + offset
            try:
                # Read 4KB chunk from RAM using GDB MI command
                # This is the same command format used by the working memrd implementation
                mem_cmd = f'-data-read-memory-bytes {addr} {chunk_size}'
                mem_responses = gdbmi.write(mem_cmd, timeout_sec=2.0, raise_error_on_timeout=False)

                # Parse memory dump and look for RTT signature
                memory_data = []
                for resp in mem_responses:
                    if resp.get('type') == 'result' and resp.get('message') == 'done':
                        memory = resp.get('payload', {}).get('memory', [])
                        if memory and len(memory) > 0:
                            # Contents are in hex string format (e.g., "53454747455220525454")
                            contents = memory[0].get('contents', '')
                            # Convert hex string to bytes
                            if contents:
                                memory_data = bytes.fromhex(contents)
                                break

                # Search for RTT signature in this chunk
                if len(memory_data) >= len(rtt_signature):
                    sig_index = memory_data.find(rtt_signature)
                    if sig_index != -1:
                        rtt_address = hex(addr + sig_index)
                        logger.info(f'Found RTT control block at {rtt_address}')
                        break
            except Exception as chunk_error:
                logger.debug(f'Error searching RAM at {addr:#x}: {chunk_error}')
                continue

        if rtt_address:
            # Tell J-Link where the RTT control block is located
            set_rtt_cmd = f'monitor exec SetRTTAddr {rtt_address}'
            logger.info(f'Setting RTT address: {set_rtt_cmd}')
            set_responses = gdbmi.write(set_rtt_cmd, timeout_sec=2.0, raise_error_on_timeout=False)
            for resp in set_responses:
                if resp.get('type') == 'console':
                    logger.info(f'SetRTTAddr response: {resp.get("payload", "")}')

            result['found'] = True
            result['address'] = rtt_address
            logger.info(f'RTT configured successfully at address {rtt_address}')
        else:
            logger.info('No RTT control block found in RAM (firmware may not use RTT or not initialized yet)')
            result['error'] = 'RTT control block not found in RAM'

        # If we connected in the all-stop fallback (JLinkGDBServer rejects
        # non-stop), the GDB memory reads above implicitly halt the core and
        # nothing resumes it -- which leaves the device halted after
        # `gdbserver --rtt` (a regression vs the non-stop path, which never
        # halts). Resume the core so RTT actually streams. No-op on the
        # non-stop path: that controller's core was never halted, so we skip
        # the resume there and leave a running target untouched.
        if not getattr(gdbmi, 'lager_non_stop', True):
            logger.info('RTT detect ran in all-stop; resuming core after RAM scan')
            gdbmi.write('monitor go', timeout_sec=2.0, raise_error_on_timeout=False)

    except Exception as e:
        logger.warning(f'RTT auto-detection failed: {e}')
        result['error'] = str(e)

    return result


@holds_probe('connect')
def connect_jlink(speed, device, transport, force=False, ignore_if_connected=False,
                  vardefs=None, attach='attach', idcode=None, serial=None,
                  gdb_port=2331, rtt_telnet_port=9090, script_file=None):
    """
    Connect to target via J-Link

    Args:
        speed: Interface speed (in kHz) or 'adaptive'
        device: J-Link device name (e.g., 'NRF52840_XXAA', 'R7FA0E107')
        transport: Transport protocol ('SWD' or 'JTAG')
        force: Force connection even if already connected
        ignore_if_connected: Return success if already connected
        vardefs: List of (varname, varvalue) tuples for additional settings
        attach: Attach mode ('attach', 'reset', 'reset-halt')
        idcode: 16-byte IDCODE for Renesas locked devices (hex string)
        serial: J-Link USB serial. None falls back to the legacy single-probe path.
        gdb_port: GDB server port to bind (default: 2331).
        rtt_telnet_port: RTT telnet port to bind (default: 9090).
        script_file: Optional path to J-Link script file to apply on connect.

    Returns:
        Status dictionary

    Raises:
        JLinkAlreadyRunningError: If J-Link is running and force=False
        JLinkStartError: If J-Link fails to start
    """
    if vardefs is None:
        vardefs = []

    # Check both PID files - CLI uses gdbserver path, Python API uses legacy path
    status = get_jlink_status(serial=serial, gdb_port=gdb_port)
    gdbserver_status = get_jlink_gdbserver_status(serial=serial)

    # Consider J-Link running if either path shows it running
    jlink_running = status['running'] or gdbserver_status['running']

    if jlink_running and ignore_if_connected:
        return {'already_running': 'ok'}

    if jlink_running and not force:
        raise JLinkAlreadyRunningError()

    # Stop both code paths to ensure clean state
    stop_jlink(serial=serial)
    stop_jlink_gdbserver(serial=serial)

    # Give hardware time to settle after disconnect to prevent fatigue
    # This prevents "Cannot connect to J-Link" errors during rapid operations
    time.sleep(0.3)  # Reduced from 0.5s - minimum USB release time

    # Set default speed if not provided
    if speed is None:
        speed = '4000'

    # Validate speed parameter
    try:
        speed = validate_speed(speed)
    except ValueError as e:
        raise DebugError(str(e))

    # Try multiple speeds if initial connection fails
    # Start with requested speed, then fall back to slower speeds if it fails
    speeds_to_try = []
    requested_speed = speed  # Save the originally requested speed

    if speed == 'adaptive':
        # For adaptive, try it first, then fall back to known-good speeds
        speeds_to_try = [speed, '4000', '1000', '500', '100']
    elif int(speed) > 1000:
        # For high speeds, try requested, then progressively slower fallbacks
        speeds_to_try = [speed, '1000', '500', '100']
    elif int(speed) > 500:
        # For medium-high speeds, try requested, then slower fallbacks
        speeds_to_try = [speed, '500', '100']
    elif int(speed) > 100:
        # For medium speeds, try requested, then 100 as fallback
        speeds_to_try = [speed, '100']
    else:
        # For speeds <= 100, just use the requested speed
        speeds_to_try = [speed]

    # Remove duplicates while preserving order
    seen = set()
    speeds_to_try = [s for s in speeds_to_try if not (s in seen or seen.add(s))]

    last_error = None

    # Determine halt mode based on attach parameter
    # Don't try multiple halt modes - use what the user requested
    if attach == 'reset-halt':
        halt_mode = '-halt'
    else:
        halt_mode = '-nohalt'

    def _try_speed_ladder(attempt_script):
        """Walk the speed ladder with *attempt_script*; status dict, or None."""
        nonlocal last_error
        for attempt_speed in speeds_to_try:
            if len(speeds_to_try) > 1:
                logger.debug(f'Attempting connection at {attempt_speed} kHz...')

            # Use the same start_jlink_gdbserver() function as the CLI
            # This ensures consistent behavior between Python API and CLI
            halt = (attach == 'reset-halt')

            try:
                result = start_jlink_gdbserver(
                    device=device,
                    speed=attempt_speed,
                    transport=transport,
                    halt=halt,
                    gdb_port=gdb_port,
                    rtt_telnet_port=rtt_telnet_port,
                    serial=serial,
                    script_file=attempt_script,
                )
            except Exception as exc:
                last_error = JLinkStartError(b'', str(exc).encode(), str(exc))
                continue

            # Check if gdbserver started successfully
            gdbserver_status = get_jlink_gdbserver_status(serial=serial)
            if gdbserver_status['running']:
                status = {
                    'running': True,
                    'start': 'ok',
                    'speed': attempt_speed,
                    'requested_speed': requested_speed,
                    'fallback_used': (attempt_speed != requested_speed),
                    'pid': result.get('pid'),
                    'gdb_port': result.get('gdb_port', gdb_port),
                    'rtt_telnet_port': result.get('rtt_telnet_port', rtt_telnet_port),
                    'serial': serial,
                }

                # Give J-Link GDB server additional time to start accepting connections
                logger.debug('Waiting for J-Link GDB server to be ready for connections...')
                time.sleep(1.0)

                # Perform reset if requested (after server is confirmed ready)
                if attach == 'reset-halt' or attach == 'reset':
                    try:
                        gdb_reset(halt=halt, device=device, port=gdb_port)
                    except Exception as e:
                        logger.warning(f'Reset after connect failed: {e}')

                # Confirm the target answers, not merely that the server does.
                #
                # This used to issue `monitor version` and treat any console
                # response as verification -- but that is the gdbserver
                # replying about itself, which it does happily with no part
                # attached. It also went nowhere: nothing read the field, and
                # `/debug/connect` reaches `start_jlink_gdbserver` directly
                # without passing through here.
                #
                # Now it is the same predicate `/debug/status` reports, so
                # there is one definition of "attached" rather than two.
                # Imported inside the function: target_probe imports this
                # module for the attach-failure predicates.
                from .target_probe import target_attached
                status['target_verified'] = target_attached(
                    'jlink', serial, gdb_port=gdb_port, device=device, probe=True,
                )

                return status
            else:
                # Connection failed at this speed, try next
                stop_jlink_gdbserver(serial=serial)
                time.sleep(0.5)
                last_error = JLinkStartError(b'', b'Connection failed', 'GDB server failed to start')
        return None

    # A user .JLinkScript that defines InitTarget() REPLACES J-Link's built-in
    # per-device InitTarget() -- the replacement is per function, so a script
    # defining no InitTarget() is harmless. On an nRF5340 that built-in is what
    # brings the DAP up on a blank part: measured at ~425ms of real work after a
    # chip erase, against ~3us for a user stub that just returns. Displaced, the
    # attach that follows an erase fails with "Could not read CPUID register"
    # and "Failed to power up DAP" -- and because flash erases by default, one
    # scripted flash could leave the part blank and the net unusable.
    #
    # So: exhaust the speed ladder WITH the script, and only then drop it and
    # try once more. Ordering matters -- dropping the user's script is the more
    # surprising change of behaviour, so it goes last, and it is reported
    # loudly rather than silently succeeding. Issue #195.
    script_attempts = [script_file, None] if script_file else [None]
    for attempt_script in script_attempts:
        status = _try_speed_ladder(attempt_script)
        if status is None:
            continue
        if script_file and attempt_script is None:
            status['script_skipped'] = script_file
            logger.warning(
                'Attached to %s WITHOUT its configured J-Link script (%s). A '
                'user .JLinkScript that defines InitTarget() replaces the '
                'device built-in that brings up a blank or protected part, so '
                'the attach only succeeded once the script was dropped. The '
                'target is NOT running your script init.',
                device, script_file,
            )
        return status

    # All attempts failed
    stop_jlink_gdbserver(serial=serial)
    # Read the J-Link server's real logfile from disk — it holds the actual
    # reason (e.g. "Failed to power up DAP", "Failed to open listener port 2331").
    # `status` is only bound on the success path, so the old `status.get('logfile')`
    # here was always the 'No log available' fallback, which hid the cause. The
    # logfile persists on disk after stop_jlink_gdbserver() kills the process.
    logfile_content = None
    try:
        with open(jlink_gdbserver_logfile(serial)) as _logf:
            logfile_content = _logf.read()
    except OSError:
        logfile_content = None
    if not logfile_content and last_error is not None:
        # Fallback: the last start error carries the log when the server process
        # itself exited (rather than starting but failing to reach the target).
        logfile_content = getattr(last_error, 'logfile', None) or None
    logfile_content = logfile_content or 'No log available'
    logfile_content_clean = clean_logfile_content(logfile_content)

    # Check for locked device (Renesas-specific but keep for compatibility)
    if 'Locked Renesas device detected' in logfile_content_clean or 'IDCODE' in logfile_content_clean:
        error_msg = (
            "ERROR: Device is LOCKED\n\n"
            "The target device has ID Code Protection enabled and requires an IDCODE to unlock.\n\n"
            "For Renesas devices, to unlock:\n"
            "1. Use Renesas Flash Programmer (RFP) to unlock the device\n"
            "   - Download from: https://www.renesas.com/software-tool/renesas-flash-programmer-programming-gui\n"
            "   - Connect via J-Link and select 'ID Authentication'\n"
            "   - WARNING: Unlocking will ERASE all flash memory\n\n"
            "2. Or provide the correct 16-byte IDCODE if you have it\n\n"
            f"Device: {device}\n"
            f"Transport: {transport}\n"
        )
    else:
        if len(speeds_to_try) > 1:
            # Provide device-specific pin information for R7FA0E107
            pin_info = ""
            if device.startswith('R7FA0E1'):
                pin_info = (
                    "\nFor R7FA0E107 (RA0E1), verify SWD connections:\n"
                    "  - SWDIO: P108 (pin 15 on 32-pin package)\n"
                    "  - SWCLK: P300 (pin 8 on 32-pin package)\n"
                    "  - RESET: P213 (pin 1) - MUST have 10k pull-up to VCC\n\n"
                )

            error_msg = (
                f"Cannot connect to target device (tried {', '.join(str(s) for s in speeds_to_try)} kHz).\n\n"
                "The J-Link probe was found, but cannot establish communication with the target MCU.\n"
                f"{pin_info}"
                "TROUBLESHOOTING CHECKLIST:\n"
                "1. Verify power and connections\n"
                "   - Check target voltage is present and stable (use multimeter)\n"
                "   - Verify debug interface pins (SWDIO/SWCLK or JTAG)\n"
                "   - Check for cold solder joints or poor connections\n"
                "   - Ensure ground connection between J-Link and target\n\n"
                "2. Reset pin issues\n"
                "   - Ensure RESET/nRST has proper pull-up resistor (typically 10k)\n"
                "   - Verify nothing is holding RESET LOW\n"
                "   - Try connecting RESET pin to J-Link's RESET output\n\n"
                "3. Device protection or sleep mode\n"
                "   - Device may have debug protection/readout protection enabled\n"
                "   - Device may be in deep sleep or low power mode\n"
                "   - Try power cycling the target\n"
                "   - Check if SWD pins are configured for alternate functions\n\n"
                f"Full J-Link log:\n{logfile_content_clean}"
            )
        else:
            error_msg = f"J-Link server failed to start or connect to target.\n\nLog output:\n{logfile_content_clean}"

    raise JLinkStartError(b'', error_msg.encode(), logfile_content_clean)


def connect(interface, speed, device, transport, **kwargs):
    """
    Connect to debug target (J-Link only)

    Args:
        interface: Must be 'third-party' for J-Link
        speed: Interface speed
        device: Device name
        transport: Transport protocol
        **kwargs: Additional arguments passed to connect function

    Returns:
        Status dictionary

    Raises:
        DebugError: If non-J-Link interface specified
    """
    if interface != 'third-party':
        raise DebugError(f"Only J-Link (third-party) interface supported. Got: {interface}")
    return connect_jlink(speed, device, transport, **kwargs)


@holds_probe('disconnect')
def disconnect(mcu=None, keep_jlink_running=False, serial=None, gdb_port=2331):
    """
    Disconnect from debug target (J-Link only)

    Args:
        mcu: MCU identifier (optional, unused for J-Link)
        keep_jlink_running: If True, only disconnect GDB client but leave J-Link running.
                           This allows external GDB clients to connect.
        serial: J-Link USB serial. None operates on the legacy single-probe state.
        gdb_port: GDB server port (default: 2331).

    Returns:
        Status dictionary
    """
    from .gdb import disconnect_gdb_client

    if keep_jlink_running:
        # Only disconnect the debug service's GDB client, leave J-Link running
        was_connected = disconnect_gdb_client(device=mcu, port=gdb_port)
        return {
            'stop': 'ok',
            'gdb_client_disconnected': was_connected,
            'jlink_still_running': True
        }
    else:
        # Original behavior: stop everything
        # Check both PID files (CLI uses gdbserver, Python API uses legacy)
        jlink_status = get_jlink_status(serial=serial, gdb_port=gdb_port)
        gdbserver_status = get_jlink_gdbserver_status(serial=serial)

        if jlink_status['running'] or gdbserver_status['running']:
            # First disconnect GDB client
            disconnect_gdb_client(device=mcu, port=gdb_port)
            # Stop both code paths to ensure clean state
            stop_jlink(serial=serial)
            stop_jlink_gdbserver(serial=serial)
            return {'stop': 'ok'}

        return {'stop': 'ok'}


@holds_probe('reset')
def reset_device(halt=False, mcu=None, serial=None, gdb_port=2331, script_file=None):
    """
    Reset connected device (J-Link only)

    Args:
        halt: Whether to halt after reset
        mcu: MCU identifier (optional, unused for J-Link)
        serial: J-Link USB serial. None operates on the legacy single-probe state.
        gdb_port: GDB server port (default: 2331).

    Returns:
        Generator yielding output from reset operation

    Raises:
        JLinkNotRunning: If J-Link is not running
    """
    # Try legacy path first (uses /tmp/jlink.pid)
    jlink_status = get_jlink_status(serial=serial, gdb_port=gdb_port)
    if jlink_status['running'] and jlink_status.get('cmdline'):
        try:
            jlink = JLink(jlink_status['cmdline'], script_file=script_file, serial=serial)
            return jlink.reset(halt)
        except (ValueError, KeyError):
            pass  # Fall through to gdbserver path

    # Try gdbserver path (uses /tmp/jlink_gdbserver.pid)
    gdbserver_status = get_jlink_gdbserver_status(serial=serial)
    if gdbserver_status['running']:
        pid = gdbserver_status.get('pid')
        if pid:
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmdline = [part.decode() for part in f.read().split(b'\x00')]
                jlink = JLink(cmdline, script_file=script_file, serial=serial)
                return jlink.reset(halt)
            except (OSError, IOError, ValueError, KeyError):
                pass  # Fall through to error

    raise JLinkNotRunning()


@holds_probe('erase')
def erase_flash(start_addr, length, mcu=None, serial=None, gdb_port=2331, script_file=None):
    """
    Erase flash memory (J-Link only)

    Args:
        start_addr: Starting address
        length: Number of bytes to erase
        mcu: MCU identifier (optional, unused for J-Link)
        serial: J-Link USB serial. None operates on the legacy single-probe state.
        gdb_port: GDB server port (default: 2331).

    Returns:
        Generator yielding output from erase operation

    Raises:
        JLinkNotRunning: If J-Link is not running
    """
    # Check both PID files (CLI uses gdbserver, Python API uses legacy)
    jlink_status = get_jlink_status(serial=serial, gdb_port=gdb_port)
    if jlink_status['running'] and jlink_status.get('cmdline'):
        try:
            jlink = JLink(jlink_status['cmdline'], script_file=script_file, serial=serial)
            return jlink.erase(start_addr, length)
        except (ValueError, KeyError):
            pass  # Fall through to gdbserver path

    gdbserver_status = get_jlink_gdbserver_status(serial=serial)
    if gdbserver_status['running']:
        pid = gdbserver_status.get('pid')
        if pid:
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmdline = [part.decode() for part in f.read().split(b'\x00')]
                jlink = JLink(cmdline, script_file=script_file, serial=serial)
                return jlink.erase(start_addr, length)
            except (OSError, IOError, ValueError, KeyError):
                pass  # Fall through to error

    raise JLinkNotRunning()


def _resolve_script_path(script_file):
    """The script path :func:`chip_erase` hands Commander, or None when there is none.

    A bare path parameter with a None default, reachable from every caller of
    this module. Contained here rather than trusted: the path must live under
    ``probes.RUNTIME_DIR``, and a path to a file that is not on disk is no
    script at all. See lager.util.paths.
    """
    if not script_file:
        return None
    # One definition, normpath, a direct startswith that dominates the use:
    # the shape CodeQL recognizes as a barrier (see lager.util.paths).
    path = os.path.normpath(script_file)
    if not path.startswith(_probes.RUNTIME_DIR + os.sep):
        raise ValueError(
            f'refusing a script path outside {_probes.RUNTIME_DIR!r}')
    return path if os.path.exists(path) else None


def jlink_erase_plan(device, script_file=None, *, start=None, length=None):
    """The range :func:`chip_erase` erases for these inputs: ``(start, length, source)``,
    or None for a full chip.

    Resolved from the same inputs, with the same script rules, as ``chip_erase``
    itself, so a caller that reports the range reads it BEFORE the erase, while
    the per-net script is still where the request left it. Resolving after the
    erase once reported ``default`` for an erase that ran the script's range: a
    concurrent disconnect on the same net had cleared the script in between.
    """
    from . import jlink as _jlink
    return _jlink.resolve_erase_range(device, _resolve_script_path(script_file), start, length)


@holds_probe('erase')
def chip_erase(device, speed='4000', transport='SWD', mcu=None, script_file=None,
               serial=None, *, start=None, length=None):
    """
    Erase flash via J-Link Commander.

    Most devices: full chip ``erase``. **DA1469x** uses **address-range** erase over the
    external QSPI XIP map (default 1 MiB @ 0x16000000 — loader-style bank 0 — or
    ``LAGER_ERASE_RANGE`` in the J-Link script). Commander enables that QSPI flash bank
    (``SetEnableFlashbank``), then unlocks external erase (``EnableEraseAllFlashBanks``),
    then ``erase <start> <end>`` — not a global chip erase — to avoid wiping internal
    flash. No extra Commander steps after the range erase (connect, erase, disconnect).

    A *start* / *length* pair is an explicit range, in absolute addresses, and takes
    precedence over the script line and the default on every device; on a DA1469x it
    must lie inside the QSPI XIP window. It is checked here, before the probe's other
    sessions are stopped, and the first line yielded then names the range.

    WARNING: On non-DA1469 devices, full chip erase erases ALL data on the chip.

    Args:
        device: J-Link device name (e.g., 'R7FA0E107', 'NRF52840_XXAA')
        speed: Interface speed in kHz (default: 4000)
        transport: Transport protocol ('SWD' or 'JTAG', default: 'SWD')
        mcu: MCU identifier (optional, unused for J-Link)
        script_file: Optional path to J-Link script (from debug service); if None,
            uses a temp file left by connect or api._get_script_file().
        serial: J-Link USB serial. None falls back to the legacy single-probe path.
        start: First address to erase, given together with *length*, or None.
        length: Number of bytes to erase, given together with *start*, or None.

    Returns:
        Generator yielding output from erase operation

    Raises:
        JLinkStartError: If J-Link fails to start
        ValueError: for a range the device cannot erase, before anything is touched
    """
    # Lazy import to avoid circular dependencies
    from . import jlink as _jlink
    from .jlink import JLink
    from .erase_bounds import format_bounds, validate_bounds

    if (start is None) != (length is None):
        raise ValueError('chip_erase() takes both start and length, or neither')
    if start is not None:
        validate_bounds(start, length, da1469x=_probes.is_da1469x(device))

    # Stop running J-Link processes for *this* probe to free its USB handle for JLinkExe.
    # Legacy start_jlink() uses /tmp/jlink.pid (or per-serial when *serial* is set);
    # the debug service uses JLinkGDBServer (/tmp/jlink_gdbserver.pid or per-serial).
    # Both must be stopped or JLinkExe cannot get exclusive USB access.
    stop_jlink(serial=serial)
    stop_jlink_gdbserver(serial=serial)

    # Give the hardware time to be released
    time.sleep(0.5)

    # Build command args for JLinkExe (used by JLink class)
    cmd_args = [
        '-device', device,
        '-if', transport,
        '-speed', str(speed)
    ]

    # Create JLink instance with command args
    # Note: This will use JLinkExe, not GDB server
    class TempJLink:
        def __init__(self, args, script_file=None, serial=None):
            self.args = args
            self.script_file = script_file
            self.serial = serial

    # The same containment jlink_erase_plan() applies, so the plan a caller
    # reported and the script Commander runs under come from one rule.
    resolved_script = _resolve_script_path(script_file)
    if not resolved_script:
        logger.warning(
            'chip_erase: no J-Link script file; DA1469x external QSPI may not be erased'
        )

    jlink = TempJLink(cmd_args, script_file=resolved_script, serial=serial)
    jlink.__class__ = JLink

    # Name the range first, the way the OpenOCD path does, so a caller that
    # joins the output (the service, DebugNet.erase()) shows what was erased.
    resolved = _jlink.resolve_erase_range(device, resolved_script, start, length)

    def _lines():
        if resolved is not None:
            yield f'Erasing {format_bounds(resolved[0], resolved[1])}'
        yield from jlink.chip_erase(start=start, length=length)

    return _lines()


@holds_probe('flash')
def flash_device(files, preverify=False, verify=True, run_after=False, mcu=None, use_gdb=True,
                 script_file=None, serial=None, gdb_port=2331, rtt_telnet_port=9090,
                 swo_port=None, telnet_port=None):
    """
    Flash firmware to device using JLinkExe.

    Note: The use_gdb parameter is deprecated and ignored. Flash always uses JLinkExe
    for reliability. The GDB-based flash method was removed due to unreliable behavior
    where it would report success but not actually program the device.

    For DA1469x, before ``loadfile`` Commander runs ``rnh`` — brief sleep — ``h`` (disable
    with ``LAGER_DA1469_PRE_FLASH_RUN_HALT=0``). Post-flash may run the target via GDB then
    stop the server. Other devices: reconnect GDB server only.

    Args:
        files: Tuple of (hexfiles, binfiles, elffiles)
        preverify: Verify before flashing (unused)
        verify: Verify after flashing (unused)
        run_after: Reset and run after flashing (JLinkExe does this automatically)
        mcu: MCU identifier (e.g., 'nRF52833_XXAA')
        use_gdb: DEPRECATED - ignored, always uses JLinkExe
        script_file: Optional path to J-Link script file (from debug service)
        serial: J-Link USB serial. None falls back to the legacy single-probe path.
        gdb_port: GDB server port to bind on the post-flash reconnect (default: 2331).
        rtt_telnet_port: RTT telnet port to bind on the post-flash reconnect (default: 9090).
        swo_port: SWO port for the post-flash reconnect. None means ``gdb_port + 1``.
        telnet_port: Telnet port for the post-flash reconnect. None means ``gdb_port + 2``.

    Returns:
        Generator yielding output from flash operation. Its return value
        (``StopIteration.value``) is the line showing nothing was programmed,
        or None; a plain ``for`` loop ignores it.
    """
    from .jlink import JLink

    # Stop running J-Link processes for *this* probe to free its USB handle for JLinkExe.
    # Both the legacy path (jlink.pid / per-serial) and JLinkGDBServer
    # (jlink_gdbserver.pid / per-serial) must be stopped; otherwise JLinkExe
    # cannot get exclusive USB access.
    stop_jlink(serial=serial)
    stop_jlink_gdbserver(serial=serial)

    # Give the hardware time to be released
    time.sleep(0.5)

    hexfiles, binfiles, elffiles = files

    # Always use JLinkExe for reliable flashing

    # Build J-Link args from mcu parameter and defaults
    device = mcu if mcu else 'nRF52833_XXAA'
    speed = '4000'  # Default speed
    transport = 'SWD'  # Default transport

    jlink_args = ['-device', device, '-if', transport, '-speed', speed]

    yield f"Flashing device {device} via JLinkExe..."

    # Create JLink instance with extracted args
    class TempJLink:
        def __init__(self, args, script_file=None, serial=None):
            self.args = args
            self.script_file = script_file
            self.serial = serial

    # A bare path parameter with a None default, reachable from every caller
    # of this module. Contain it here rather than trusting the caller: the
    # check has to sit in the function that uses the path for it to mean
    # anything locally. See lager.util.paths.
    if script_file:
        script_file = os.path.normpath(script_file)
        if not script_file.startswith(_probes.RUNTIME_DIR + os.sep):
            raise ValueError(
                f'refusing a script path outside {_probes.RUNTIME_DIR!r}')
    resolved_script = script_file if (script_file and os.path.exists(script_file)) else None

    def _run_flash(script):
        jl = TempJLink(jlink_args, script_file=script, serial=serial)
        jl.__class__ = JLink
        output = []
        try:
            for chunk in jl.flash(files, preverify, verify):
                output.append(str(chunk))
        except _jlink_mod.JLinkCommanderExited as exc:
            # The probe went away mid-session. Kept as a line of output so the
            # verdict names it and the diagnosis and reset-skip below still run.
            output.append(str(exc))
        return output

    # Same defect as the gdbserver attach, but this is J-Link Commander, which
    # reports a failed connect as TEXT in its output rather than by raising --
    # so it needs its own retry and its own detection. This is the path that
    # actually fails after `flash` erases: the erase blanks the part, and the
    # Commander connect that follows cannot attach with the device's
    # InitTarget() displaced by the user's. Issue #195.
    sampler = _JLinkClientSampler(serial)
    with sampler:
        flash_output = _run_flash(resolved_script)
    if resolved_script and _connect_failed(flash_output):
        logger.warning(
            'Flash could not attach with the J-Link script %s; retrying '
            'without it.', resolved_script,
        )
        with sampler:
            retry_output = _run_flash(None)
        if not _connect_failed(retry_output):
            yield (f'WARNING: could not attach with the configured J-Link script '
                   f'({resolved_script}), so it was SKIPPED for this flash. A '
                   f'script defining InitTarget() replaces the device built-in '
                   f'that brings up a blank part, and flash erases first. The '
                   f'target was programmed WITHOUT your script init. Remove it '
                   f'with `lager nets remove-script <net> --box <BOX>`.')
            flash_output = retry_output
        # Retry failed too: keep the original output, which carries the real error.

    yield from flash_output
    failure = _flash_failure(flash_output)

    time.sleep(1.0)  # Give JLinkExe time to fully disconnect

    if failure:
        # Before the post-flash steps below start a client of their own.
        yield from _flash_failure_diagnosis(device, jlink_args, resolved_script, serial,
                                            seen=sampler.seen.values())

    is_da1469 = _probes.is_da1469x(device)

    if is_da1469 and failure:
        # Nothing was programmed, so there is no application to boot, and
        # "Target reset -- bootrom will ... boot application" would read as
        # success under a failed flash.
        yield "DA1469x: programming failed; skipping the post-flash reset"
    elif is_da1469:
        # DA1469x: issue a software reset via J-Link Commander so the bootrom
        # re-initialises QSPI, clocks, and cache from scratch.  This mirrors
        # what the flash_loader GDB template does (write to SYS_CTRL_REG
        # 0x100C0050) and avoids the fragile start-gdbserver / gdb-reset /
        # stop-gdbserver dance that left the target in a state where a
        # subsequent `gdbserver --rtt` attach would freeze the application.
        yield "DA1469x: resetting target via J-Link Commander..."
        try:
            reset_args = ['-device', device, '-if', 'SWD', '-speed', speed]
            with commander(reset_args, script_file=resolved_script, serial=serial) as jl:
                jl.run_command('connect')
                # Disable MPU and MTB so the next debug attach is clean
                jl.run_command('w4 0xE000ED94 0')   # MPU_CTRL = 0
                jl.run_command('w4 0xE0043000 0')   # MTB_POSITION = 0
                jl.run_command('w4 0xE0043004 0')   # MTB_MASTER = 0
                jl.run_command('w4 0xE0043008 0')   # MTB_FLOW = 0
                # Software reset via SYS_CTRL_REG — lets bootrom run fully
                jl.run_command('w4 0x100C0050 1')
            yield "Target reset — bootrom will reinitialise and boot application"
        except Exception as e:
            logger.warning("DA1469x post-flash Commander reset failed: %s", e)
            yield f"Warning: Could not reset target after flash: {e}"
    else:
        # Non-DA1469x: reconnect GDB server so the debug service knows
        # a server is running for subsequent operations.
        yield "Reconnecting GDB server..."
        try:
            start_jlink_gdbserver(
                device=device,
                speed=speed,
                transport=transport,
                halt=False,
                gdb_port=gdb_port,
                swo_port=swo_port,
                telnet_port=telnet_port,
                rtt_telnet_port=rtt_telnet_port,
                serial=serial,
                script_file=resolved_script,
            )
            yield "GDB server reconnected"
        except Exception as e:
            yield f"Warning: Failed to reconnect GDB server: {e}"

    return failure



@holds_probe('memory read')
def read_memory(address, length, mcu=None, serial=None, gdb_port=2331, script_file=None):
    """
    Read memory from target device via J-Link monitor command

    This function uses J-Link's native monitor commands instead of GDB's examine command
    because GDB's 'x' command can be unreliable in MI mode.

    Args:
        address: Memory address to read (int or hex string)
        length: Number of bytes to read
        mcu: MCU/device name (optional)
        serial: J-Link USB serial. None operates on the legacy single-probe state.
        gdb_port: GDB server port (default: 2331).

    Returns:
        bytes: Memory contents as bytes object

    Raises:
        JLinkNotRunning: If J-Link GDB server is not running
        DebugError: If memory read fails
    """
    # Ensure J-Link is running (check both PID file paths)
    jlink_status = get_jlink_status(serial=serial, gdb_port=gdb_port)
    gdbserver_status = get_jlink_gdbserver_status(serial=serial)
    if not jlink_status['running'] and not gdbserver_status['running']:
        raise JLinkNotRunning("J-Link GDB server is not running. Call connect() first.")

    # Convert address to int if it's a string
    if isinstance(address, str):
        address = int(address, 16 if address.startswith('0x') else 10)

    try:
        # Use J-Link Commander directly for reliable memory reads
        # This bypasses GDB entirely and uses J-Link's native capabilities

        # Try legacy path first, then gdbserver path
        jlink = None
        if jlink_status['running'] and jlink_status.get('cmdline'):
            try:
                jlink = JLink(jlink_status['cmdline'], script_file=script_file, serial=serial)
            except (ValueError, KeyError):
                pass

        if jlink is None and gdbserver_status['running']:
            pid = gdbserver_status.get('pid')
            if pid:
                try:
                    with open(f'/proc/{pid}/cmdline', 'rb') as f:
                        cmdline = [part.decode() for part in f.read().split(b'\x00')]
                    jlink = JLink(cmdline, script_file=script_file, serial=serial)
                except (OSError, IOError, ValueError, KeyError):
                    pass

        if jlink is None:
            raise JLinkNotRunning("J-Link GDB server is not running. Call connect() first.")

        # Use J-Link Commander to read memory directly
        memory_data = jlink.read_memory(address, length)

        if not memory_data:
            raise DebugError("No memory data returned from J-Link")

        if len(memory_data) < length:
            raise DebugError(f"Only read {len(memory_data)} bytes, expected {length}")

        return memory_data

    except Exception as e:
        raise DebugError(f"Failed to read memory at 0x{address:08X}: {e}")


class RTT:
    """
    SEGGER Real-Time Transfer (RTT) context manager for bidirectional communication
    with embedded devices during debugging.

    RTT provides high-speed communication faster than UART with no timing impact on
    the target application.

    Usage:
        with debug_net.rtt() as rtt:
            data = rtt.read_some(timeout=1.0)
            if data:
                print(data.decode('utf-8'))
            rtt.write(b'command\\n')
    """

    def __init__(self, device=None, channel=0, search_addr=None, search_size=None, chunk_size=None,
                 serial=None, rtt_telnet_port=9090, gdb_port=2331,
                 reconnect=True, reconnect_timeout=30.0):
        """
        Initialize RTT session.

        Args:
            device: Device name (optional, for auto-detection)
            channel: RTT channel number (default: 0)
            search_addr: RAM start address for RTT control block search (default: 0x20000000)
            search_size: Size of RAM region to search in bytes (default: 0x10000 / 64KB)
            chunk_size: Size of each read chunk in bytes (default: 0x1000 / 4KB)
            serial: J-Link USB serial. None operates on the legacy single-probe state.
            rtt_telnet_port: Base RTT telnet port that the server is listening on
                (default: 9090). Channel offset is added on top.
            gdb_port: GDB server port (default: 2331).
            reconnect: When True (default), the reader transparently re-attaches
                to the same RTT telnet port if the gdbserver is bounced underneath
                it. A J-Link ``flash()`` (and ``reset()`` via a Commander grab)
                briefly frees the probe's USB and restarts the gdbserver on the
                *same* ports; without reconnect the reader's socket would EOF and
                go silent. Set False to restore the legacy one-shot behaviour.
            reconnect_timeout: Upper bound, in seconds, on how long the reader
                keeps trying to re-attach after the socket drops before giving up
                (returns ``None`` forever). Bounded on purpose: a DA1469x
                ``flash()`` deliberately leaves the gdbserver *down* (it does a
                Commander software-reset instead of a restart), so an unbounded
                poll would spin forever there.
        """
        self.device = device
        self.channel = channel
        self.search_addr = search_addr
        self.search_size = search_size
        self.chunk_size = chunk_size
        self.serial = serial
        self.gdb_port = gdb_port
        self._socket = None
        self._port = rtt_telnet_port + channel
        # Reconnect bookkeeping (see __init__ docstring). ``_reconnect_deadline``
        # is set lazily the first time the socket drops and cleared on a
        # successful re-attach so each independent drop gets a fresh budget.
        self._reconnect = reconnect
        self._reconnect_timeout = reconnect_timeout
        self._reconnect_deadline = None
        self._next_reconnect_at = 0.0

    def _server_running(self):
        """True if a J-Link gdbserver is up for this probe (either PID regime)."""
        status = get_jlink_status(serial=self.serial, gdb_port=self.gdb_port)
        gdbserver_status = get_jlink_gdbserver_status(serial=self.serial)
        return bool(status['running'] or gdbserver_status['running'])

    def _detect_rtt(self):
        """Re-run RTT control-block auto-detection (best effort, never raises).

        Must run on every (re)attach: a restarted gdbserver has no idea where
        the RTT control block lives until ``monitor exec SetRTTAddr`` is issued
        again, so skipping this after a flash would give a connected-but-silent
        socket.
        """
        rtt_kwargs = {
            'device_type': self.device,
            'serial': self.serial,
            'gdb_port': self.gdb_port,
        }
        if self.search_addr is not None:
            rtt_kwargs['search_addr'] = self.search_addr
        if self.search_size is not None:
            rtt_kwargs['search_size'] = self.search_size
        if self.chunk_size is not None:
            rtt_kwargs['chunk_size'] = self.chunk_size
        try:
            rtt_result = detect_and_configure_rtt(**rtt_kwargs)
        except Exception as exc:  # noqa: BLE001 — detection is advisory
            logger.warning(f"RTT auto-detection error: {exc}")
            return
        if rtt_result['found']:
            logger.info(f"RTT control block found at {rtt_result['address']}")
        elif rtt_result['error']:
            logger.warning(f"RTT auto-detection warning: {rtt_result['error']}")

    def _open_socket(self, max_retries, retry_delay):
        """Connect to the RTT telnet port. Returns True on success, else False.

        Tries IPv6 (``::1``) then IPv4 (``127.0.0.1``) — J-Link may bind either
        depending on host config. Assigns ``self._socket`` only on success so a
        failed attempt never leaves a half-open handle behind.
        """
        import socket
        import time

        delay = retry_delay
        for attempt in range(max_retries):
            for family, addr in [(socket.AF_INET6, '::1'), (socket.AF_INET, '127.0.0.1')]:
                sock = None
                try:
                    sock = socket.socket(family, socket.SOCK_STREAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.settimeout(2.0)
                    sock.connect((addr, self._port))
                    self._socket = sock
                    logger.info(f"RTT connected using {addr}:{self._port}")
                    return True
                except (ConnectionRefusedError, socket.timeout, OSError):
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 1.5
        return False

    def __enter__(self):
        """Enter RTT context - establish connection"""
        # Check if debugger is connected (check both PID file paths)
        if not self._server_running():
            raise JLinkNotRunning("J-Link must be connected before using RTT")

        # Auto-detect and configure RTT control block
        self._detect_rtt()

        # Connect to J-Link RTT telnet server with retry logic
        max_retries = 5 if self.channel == 0 else 1
        if self._open_socket(max_retries=max_retries, retry_delay=0.5):
            return self

        if self.channel == 0:
            raise DebugError(
                f'Cannot connect to RTT (port {self._port}). Device may not have RTT initialized.'
            )
        raise DebugError(f'RTT channel {self.channel} not available. Try channel 0.')

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit RTT context - close connection"""
        self._close_socket()
        return False

    def _close_socket(self):
        """Drop the current socket without disturbing reconnect bookkeeping."""
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def _try_reconnect(self):
        """Best-effort re-attach to the RTT telnet port after a drop.

        Returns True only when a fresh socket is established. Honours a backoff
        (so we don't hammer the port) and an overall deadline (so a flash that
        leaves the server down doesn't make the reader spin forever). Only
        attempts a connect when a gdbserver is actually back up — it never
        starts or restarts one itself, so it can't disturb a live session.
        """
        import time

        if not self._reconnect:
            return False

        now = time.monotonic()
        if self._reconnect_deadline is None:
            self._reconnect_deadline = now + self._reconnect_timeout
        if now > self._reconnect_deadline:
            return False
        if now < self._next_reconnect_at:
            return False
        self._next_reconnect_at = now + 0.5

        if not self._server_running():
            return False

        # Restarted server has lost the RTT address; re-detect before attaching.
        self._detect_rtt()
        if self._open_socket(max_retries=1, retry_delay=0.0):
            logger.info("RTT reader re-attached to port %s after gdbserver restart", self._port)
            self._reconnect_deadline = None
            return True
        return False

    def read_some(self, timeout=1.0):
        """
        Read available data from RTT with timeout.

        Transparently re-attaches to the RTT telnet port if the gdbserver was
        bounced underneath the reader (e.g. by ``flash()`` / ``reset()``), so a
        long-lived consumer keeps producing across a flash instead of silently
        dying. Returns ``None`` on an idle interval *or* while a reconnect is
        pending — callers already treat ``None`` as "nothing yet, try again".

        Args:
            timeout: Read timeout in seconds (default: 1.0)

        Returns:
            bytes: Data read from RTT, or None if no data / reconnecting
        """
        import select
        import time

        if self._socket is None:
            if not self._try_reconnect():
                # Avoid a hot loop while we wait for the server to come back.
                time.sleep(min(timeout, 0.25))
                return None

        # Wait for data with timeout
        ready = select.select([self._socket], [], [], timeout)
        if not ready[0]:
            return None

        try:
            data = self._socket.recv(4096)
        except Exception as e:
            logger.error(f"RTT read error: {e}")
            data = b''

        if data:
            return data

        # Empty read after select-ready == peer closed the socket. This is the
        # flash/reset gdbserver bounce: drop the dead handle and let the next
        # call re-attach to the same stable port (bounded by reconnect_timeout).
        self._close_socket()
        return None

    def write(self, data):
        """
        Write data to RTT.

        Args:
            data: bytes to send to target

        Returns:
            int: Number of bytes written
        """
        if self._socket is None and not self._try_reconnect():
            raise DebugError("RTT not connected")

        if isinstance(data, str):
            data = data.encode('utf-8')

        try:
            return self._socket.send(data)
        except Exception as e:
            logger.error(f"RTT write error: {e}")
            self._close_socket()
            raise DebugError(f"Failed to write to RTT: {e}")