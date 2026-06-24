# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
JLinkGDBServer management for debug operations

This module provides functions to start, stop, and manage JLinkGDBServer
processes for embedded debugging.
"""

import os
import subprocess
import signal
import time
import logging

from .probes import jlink_gdbserver_pidfile, jlink_gdbserver_logfile

logger = logging.getLogger(__name__)

# JLinkGDBServer paths (checked in order)
JLINK_GDB_SERVER_PATHS = [
    '/tmp/lager-jlink-bin/JLinkGDBServerCLExe',  # Symlinks to /opt/SEGGER (most common)
    '/opt/SEGGER/JLink_V794e/JLinkGDBServerCLExe',  # Direct path on newer boxes
    '/home/www-data/third_party/jlink/JLinkGDBServerCLExe',
    '/home/www-data/third_party/JLink_V884/JLinkGDBServerCLExe',
    '/home/www-data/third_party/JLink_Linux_V794a_x86_64/JLinkGDBServerCLExe',
    '/usr/bin/JLinkGDBServerCLExe',
]

# Legacy single-probe paths. Multi-probe paths are derived from probes helpers.
JLINK_PIDFILE = jlink_gdbserver_pidfile(None)
JLINK_LOGFILE = jlink_gdbserver_logfile(None)


def get_jlink_gdb_server_path():
    """Find JLinkGDBServer executable, return None if not found."""
    for path in JLINK_GDB_SERVER_PATHS:
        if os.path.exists(path):
            return path
    return None


def get_jlink_gdbserver_status(serial=None):
    """Check if JLinkGDBServer is running.

    Args:
        serial: J-Link USB serial. None reads the legacy single-probe PID file.

    Returns:
        dict: {'running': bool, 'pid': int or None}
    """
    pidfile = jlink_gdbserver_pidfile(serial)
    if not os.path.exists(pidfile):
        return {'running': False, 'pid': None}

    try:
        with open(pidfile, 'r') as f:
            pid = int(f.read().strip())

        # Check if process is alive
        try:
            os.kill(pid, 0)  # Signal 0 checks if process exists
            return {'running': True, 'pid': pid}
        except OSError:
            # Process doesn't exist, clean up stale PID file
            os.remove(pidfile)
            return {'running': False, 'pid': None}
    except Exception as e:
        logger.warning(f'Error checking JLinkGDBServer status: {e}')
        return {'running': False, 'pid': None}


def _gdbserver_pkill_pattern(serial):
    """pkill ``-f`` pattern that matches only *serial*'s gdbserver, or all when None.

    JLinkGDBServer cmdline includes ``-select USB=<serial>`` (Phase 1+) for the
    serial-aware path, so anchoring on that substring is sufficient to avoid
    killing a sibling probe's gdbserver.
    """
    if serial:
        return f'JLinkGDBServerCLExe.*USB={serial}'
    return 'JLinkGDBServerCLExe'


def _proc_cmdline(pid):
    """Return *pid*'s cmdline (NULs -> spaces), or '' if it can't be read.

    Guards ``os.killpg`` against a stale pidfile whose PID has been recycled by
    an unrelated process. Linux-only (``/proc``); returns '' on any other
    platform or on error, in which case callers proceed with the killpg path
    only when nothing contradicts it (an empty string never matches the
    "different process" check below).
    """
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return f.read().replace(b'\x00', b' ').decode('utf-8', 'replace').strip()
    except (OSError, ValueError):
        return ''


def _gdbserver_killpg_and_reap(pid, max_wait=2.0):
    """SIGTERM -> SIGKILL the gdbserver's process group, then reap zombies.

    The server is started with ``preexec_fn=os.setpgrp`` so its PGID equals
    *pid*. Killing the whole group is robust to a ``-select USB=<serial>``
    cmdline whose serial does not match the one we were asked to stop — the
    failure mode the serial-scoped ``pkill`` alone could miss. Mirrors
    ``stop_jlink`` in process.py. ``os.waitpid(-pid, WNOHANG)`` only reaps when
    we are the parent; otherwise ``ChildProcessError`` is caught and init reaps.
    """
    try:
        os.killpg(pid, signal.SIGTERM)
        logger.debug(f'Sent SIGTERM to JLinkGDBServer process group {pid}')
    except ProcessLookupError:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return  # already gone
    except PermissionError:
        logger.error(f'Permission denied signalling JLinkGDBServer group {pid}')
        return

    # Wait for graceful exit.
    waited = 0.0
    while waited < max_wait:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
        waited += 0.1

    # Force-kill the group if still alive.
    try:
        os.kill(pid, 0)
        logger.debug(f'JLinkGDBServer {pid} did not exit in {max_wait}s; sending SIGKILL')
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    except ProcessLookupError:
        pass

    # Reap zombies in our process group (no-op when we are not the parent).
    reap_start = time.time()
    while time.time() - reap_start < 2.0:
        try:
            dead_pid, _ = os.waitpid(-pid, os.WNOHANG)
            if dead_pid == 0:
                break
        except (ChildProcessError, ProcessLookupError):
            break
        except OSError as exc:
            logger.debug(f'Error reaping JLinkGDBServer zombies: {exc}')
            break


def _gdbserver_pkill(pkill_pattern):
    """Serial-scoped ``pkill`` (TERM then KILL), only when ``pgrep`` matches.

    Belt-and-suspenders for orphans the pidfile PID did not cover (stale PID, a
    server started outside our bookkeeping, or a PID that differs from the
    pidfile). Scoped to the serial when known so a sibling probe's gdbserver is
    never touched. A no-op when nothing matches (so no extra latency on the
    common success path where killpg already reaped the server).
    """
    try:
        check = subprocess.run(
            ['pgrep', '-f', pkill_pattern],
            capture_output=True,
            timeout=2.0,
            check=False,
        )
        if check.returncode != 0:
            return
    except FileNotFoundError:
        # No pgrep available: fall through to a best-effort pkill.
        pass
    except subprocess.TimeoutExpired:
        return

    logger.info('JLinkGDBServer orphan still matched; sweeping with pkill')
    try:
        subprocess.run(['pkill', '-TERM', '-f', pkill_pattern], timeout=1.0, check=False)
        time.sleep(0.5)
        subprocess.run(['pkill', '-KILL', '-f', pkill_pattern], timeout=1.0, check=False)
        time.sleep(0.2)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def stop_jlink_gdbserver(serial=None):
    """Stop the JLinkGDBServer for *serial* (or the legacy single-probe).

    Primary mechanism: read the pidfile PID and kill its whole process group
    via ``os.killpg`` (+ reap zombies). Because the server runs in its own
    group (``preexec_fn=os.setpgrp``), this reliably tears it down even when the
    ``-select USB=<serial>`` cmdline does not match *serial* — the case the old
    serial-scoped ``pkill`` could silently miss (the flash-path reap in
    ``api.py``). A serial-scoped ``pkill`` still runs afterwards to sweep any
    orphan whose PID is absent from (or differs from) the pidfile.

    Args:
        serial: J-Link USB serial. None operates on the legacy single-probe path.
    """
    pidfile = jlink_gdbserver_pidfile(serial)
    pkill_pattern = _gdbserver_pkill_pattern(serial)

    pid = None
    if os.path.exists(pidfile):
        try:
            with open(pidfile, 'r') as f:
                pid = int(f.read().strip())
        except (OSError, ValueError) as exc:
            logger.warning(f'Could not read JLinkGDBServer pidfile {pidfile}: {exc}')

    if pid is not None:
        cmdline = _proc_cmdline(pid)
        if cmdline and 'JLinkGDBServerCLExe' not in cmdline:
            # PID recycled by an unrelated process — never killpg it; let the
            # serial-scoped pkill below handle any real orphan instead.
            logger.warning(
                f'JLinkGDBServer pidfile PID {pid} now belongs to a different '
                f'process; skipping killpg and using serial-scoped pkill only'
            )
        else:
            _gdbserver_killpg_and_reap(pid)

    # Fallback sweep for orphans not covered by the pidfile PID.
    _gdbserver_pkill(pkill_pattern)

    # Remove the (now stale) PID file.
    try:
        os.remove(pidfile)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug(f'Could not remove pidfile {pidfile}: {exc}')


def start_jlink_gdbserver(device, speed='adaptive', transport='SWD', halt=False,
                          gdb_port=2331, script_file=None, serial=None,
                          rtt_telnet_port=9090, swo_port=None, telnet_port=None):
    """Start JLinkGDBServer process.

    Args:
        device: Target device name (e.g., 'R7FA0E107')
        speed: SWD/JTAG speed in kHz or 'adaptive'
        transport: Transport protocol ('SWD' or 'JTAG')
        halt: Whether to halt the device when connecting
        gdb_port: GDB server port (default: 2331)
        script_file: Optional path to J-Link script file (.JLinkScript)
        serial: J-Link USB serial. None falls back to the legacy single-probe
            path (no `-select USB=<sn>`, /tmp/jlink_gdbserver.pid).
        rtt_telnet_port: RTT telnet port (default: 9090)
        swo_port: SWO raw output port. JLinkGDBServer's hardcoded default is 2332
            regardless of -port, so when running multiple instances on different
            slots we MUST pass this explicitly to avoid collisions. None means
            ``gdb_port + 1``.
        telnet_port: Terminal I/O port. Same story — default 2333 collides across
            slots. None means ``gdb_port + 2``.

    Returns:
        dict: {'pid': int, 'status': str, 'gdb_port': int, 'swo_port': int,
               'telnet_port': int, 'rtt_telnet_port': int, 'serial': str|None}

    Raises:
        Exception: If JLinkGDBServer not found or fails to start
    """
    jlink_exe = get_jlink_gdb_server_path()
    if not jlink_exe:
        raise Exception('JLinkGDBServerCLExe not found')

    pidfile = jlink_gdbserver_pidfile(serial)
    logfile = jlink_gdbserver_logfile(serial)

    # Ensure gdb_port is free: orphan server may hold the port without a PID file.
    # Phase 1 keeps this stop broad (kills any gdbserver) — Phase 3 narrows to *this* serial.
    stop_jlink_gdbserver(serial=serial)
    time.sleep(0.15)

    # Build command arguments
    halt_arg = '-halt' if halt else '-nohalt'

    cmd = [jlink_exe, halt_arg,
           '-device', device,
           '-if', transport]

    if speed != 'adaptive':
        try:
            int(speed)  # Validate it's a number
        except ValueError:
            raise ValueError(f"Invalid speed: {speed}")
    cmd.extend(['-speed', speed])

    select_arg = f'USB={serial}' if serial else 'USB'
    if swo_port is None:
        swo_port = gdb_port + 1
    if telnet_port is None:
        telnet_port = gdb_port + 2
    cmd.extend([
        '-select', select_arg,
        '-port', str(gdb_port),
        '-swoport', str(swo_port),
        '-telnetport', str(telnet_port),
        '-RTTTelnetPort', str(rtt_telnet_port),
        '-LocalhostOnly', '0',  # Allow connections from any IP (Tailscale)
        '-stayrunning', '1',    # Keep server running
        '-nogui',
        '-logtofile',
        '-log', str(logfile)
    ])

    # Add J-Link script file if provided
    if script_file and os.path.exists(script_file):
        cmd.extend(['-JLinkScriptFile', script_file])
        logger.info(f'Using J-Link script file: {script_file}')

    logger.info(f'Starting JLinkGDBServer: {" ".join(cmd)}')

    # Start the process in the background
    with open(logfile, 'w') as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setpgrp  # Create new process group
        )

    # Write PID to file
    with open(pidfile, 'w') as f:
        f.write(str(proc.pid))

    logger.info(f'JLinkGDBServer started with PID {proc.pid}')

    # Wait for JLinkGDBServer to initialize
    time.sleep(1.0)

    # Verify the process is still running
    poll_result = proc.poll()
    if poll_result is not None:
        # Process exited - read logfile for error details
        error_msg = f'JLinkGDBServer failed to start (exit code: {poll_result})'
        try:
            with open(logfile, 'r') as log:
                log_contents = log.read()
                if log_contents:
                    error_msg += f'\n\nJLinkGDBServer output:\n{log_contents}'
        except Exception as log_err:
            logger.warning(f'Could not read logfile: {log_err}')

        # Clean up PID file
        try:
            os.remove(pidfile)
        except FileNotFoundError:
            pass

        raise Exception(error_msg)

    # Additional check: verify process exists using os.kill
    try:
        os.kill(proc.pid, 0)  # Signal 0 just checks existence
    except OSError:
        error_msg = f'JLinkGDBServer process {proc.pid} not found after startup'
        try:
            with open(logfile, 'r') as log:
                log_contents = log.read()
                if log_contents:
                    error_msg += f'\n\nJLinkGDBServer output:\n{log_contents}'
        except Exception:
            pass
        raise Exception(error_msg)

    return {
        'pid': proc.pid,
        'status': 'started',
        'gdb_port': gdb_port,
        'swo_port': swo_port,
        'telnet_port': telnet_port,
        'rtt_telnet_port': rtt_telnet_port,
        'serial': serial,
    }
