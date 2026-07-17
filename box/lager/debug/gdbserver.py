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

from .mappings import check_process
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

        # Check if the process is alive AND not a defunct zombie. A zombie
        # JLinkGDBServer (left by a flash that ran while the probe was down)
        # passes a bare os.kill(pid, 0) check and would be reused by
        # connect(ignore_if_connected=True), failing the next flash.
        if check_process(pid):
            return {'running': True, 'pid': pid}
        # Process doesn't exist or is a zombie — clean up the stale PID file so
        # connect() falls through to a fresh start_jlink_gdbserver().
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


def _free_gdb_port(gdb_port):
    """Kill any JLinkGDBServer holding *gdb_port*, regardless of probe serial.

    ``stop_jlink_gdbserver`` is serial-anchored, so it cannot reap a gdbserver
    started under a different ``-select`` tag — e.g. a bare ``-select USB``
    server left by one caller versus a ``-select USB=<serial>`` server from
    another. Two such servers then collide on the same ``-port`` and deadlock
    the probe (JLinkGDBServer logs "Failed to open listener port 2331" on one
    and "Failed to power up DAP" on the other). Reaping by the exact
    ``-port <gdb_port>`` argument frees only the port we are about to bind, which
    is correct for single- and multi-probe boxes alike (each slot owns a
    distinct port).
    """
    # Trailing space anchors the match so port 2331 does not also match 23310,
    # and ``-port`` (with the leading dash) never matches ``-swoport``/``-telnetport``.
    pattern = f'JLinkGDBServerCLExe.* -port {gdb_port} '
    try:
        check = subprocess.run(
            ['pgrep', '-f', pattern], capture_output=True, timeout=2.0, check=False
        )
        if check.returncode != 0:
            return  # nothing is holding this port
    except FileNotFoundError:
        pass  # no pgrep — fall through to best-effort pkill
    for sig in ('-TERM', '-KILL'):
        try:
            subprocess.run(['pkill', sig, '-f', pattern], timeout=1.0, check=False)
        except FileNotFoundError:
            return  # no pkill available
        time.sleep(0.2)


def stop_jlink_gdbserver(serial=None):
    """Stop JLinkGDBServer process.

    Uses pkill to terminate the process and all its children, avoiding zombies.
    When *serial* is provided, the pkill pattern is anchored on
    ``USB=<serial>`` so a sibling probe's gdbserver is not killed. When *serial*
    is None, the legacy broad ``JLinkGDBServerCLExe`` pattern is used.

    If the PID file is missing, an orphan gdbserver may still be holding the
    GDB port — try pkill when pgrep finds a matching process.

    Args:
        serial: J-Link USB serial. None operates on the legacy single-probe path.
    """
    pidfile = jlink_gdbserver_pidfile(serial)
    pkill_pattern = _gdbserver_pkill_pattern(serial)
    if not os.path.exists(pidfile):
        should_pkill = False
        try:
            check = subprocess.run(
                ['pgrep', '-f', pkill_pattern],
                capture_output=True,
                timeout=2.0,
                check=False,
            )
            should_pkill = check.returncode == 0
        except FileNotFoundError:
            should_pkill = True  # no pgrep: best-effort pkill only
        if not should_pkill:
            logger.debug('JLinkGDBServer PID file missing and no matching process')
            return
        logger.info('JLinkGDBServer has no PID file but process exists; stopping orphan')
        try:
            subprocess.run(
                ['pkill', '-TERM', '-f', pkill_pattern],
                timeout=1.0,
                check=False,
            )
            time.sleep(0.5)
            subprocess.run(
                ['pkill', '-KILL', '-f', pkill_pattern],
                timeout=1.0,
                check=False,
            )
            time.sleep(0.2)
        except FileNotFoundError:
            pass
        return

    try:
        with open(pidfile, 'r') as f:
            pid = int(f.read().strip())

        # Use pkill to kill by name - this avoids zombie issues
        # and ensures all children are terminated. Pattern is per-serial when
        # known so we don't kill a sibling probe's gdbserver.
        try:
            subprocess.run(
                ['pkill', '-TERM', '-f', pkill_pattern],
                timeout=1.0,
                check=False
            )
            logger.debug('Sent SIGTERM to JLinkGDBServer via pkill')
        except subprocess.TimeoutExpired:
            logger.warning('pkill timed out')
        except FileNotFoundError:
            # pkill not available, fall back to kill
            logger.debug('pkill not available, using kill')
            try:
                os.kill(pid, signal.SIGTERM)
                logger.debug(f'Sent SIGTERM to JLinkGDBServer process {pid}')
            except ProcessLookupError:
                logger.debug(f'JLinkGDBServer process {pid} not found')
                os.remove(pidfile)
                return

        # Wait for the process to exit gracefully
        max_wait = 2.0  # seconds
        wait_interval = 0.1
        waited = 0.0

        while waited < max_wait:
            try:
                os.kill(pid, 0)  # Check if still alive
                time.sleep(wait_interval)
                waited += wait_interval
            except ProcessLookupError:
                logger.debug(f'JLinkGDBServer process {pid} exited gracefully')
                break

        # Force kill if still running after graceful period
        try:
            os.kill(pid, 0)  # Check if still alive
            logger.debug(f'JLinkGDBServer process {pid} did not exit after {max_wait}s, forcing SIGKILL')
            subprocess.run(
                ['pkill', '-KILL', '-f', pkill_pattern],
                timeout=1.0,
                check=False
            )
            time.sleep(0.2)
        except ProcessLookupError:
            logger.debug(f'JLinkGDBServer process {pid} successfully terminated')
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # Fallback to direct kill
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        # Remove PID file
        try:
            os.remove(pidfile)
        except FileNotFoundError:
            pass

    except Exception as exc:
        logger.error(f'Failed to stop JLinkGDBServer: {exc}')


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

    # Ensure gdb_port is free before we bind it. The serial-anchored stop below
    # only reaps *this* serial's server; a server left under a different -select
    # tag would keep the port and deadlock the probe. _free_gdb_port() then reaps
    # whatever holds this exact port so the two cannot collide.
    stop_jlink_gdbserver(serial=serial)
    _free_gdb_port(gdb_port)
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
    cmd.extend(['-speed', str(speed)])

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
