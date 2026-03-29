# Copyright 2024-2026 Lager Data LLC
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

# J-Link PID and log files
JLINK_PIDFILE = '/tmp/jlink_gdbserver.pid'
JLINK_LOGFILE = '/tmp/jlink_gdbserver.log'


def get_jlink_gdb_server_path():
    """Find JLinkGDBServer executable, return None if not found."""
    for path in JLINK_GDB_SERVER_PATHS:
        if os.path.exists(path):
            return path
    return None


def get_jlink_gdbserver_status():
    """Check if JLinkGDBServer is running.

    Returns:
        dict: {'running': bool, 'pid': int or None}
    """
    if not os.path.exists(JLINK_PIDFILE):
        return {'running': False, 'pid': None}

    try:
        with open(JLINK_PIDFILE, 'r') as f:
            pid = int(f.read().strip())

        # Check if process is alive
        try:
            os.kill(pid, 0)  # Signal 0 checks if process exists
            return {'running': True, 'pid': pid}
        except OSError:
            # Process doesn't exist, clean up stale PID file
            os.remove(JLINK_PIDFILE)
            return {'running': False, 'pid': None}
    except Exception as e:
        logger.warning(f'Error checking JLinkGDBServer status: {e}')
        return {'running': False, 'pid': None}


def stop_jlink_gdbserver():
    """Stop JLinkGDBServer process.

    Uses pkill to properly terminate the process and all its children,
    avoiding zombie processes.

    If the PID file is missing, a JLinkGDBServer may still be running (crash,
    manual start, or stale cleanup) and will keep the GDB port busy — try pkill
    when pgrep finds a process.
    """
    if not os.path.exists(JLINK_PIDFILE):
        should_pkill = False
        try:
            check = subprocess.run(
                ['pgrep', '-f', 'JLinkGDBServerCLExe'],
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
                ['pkill', '-TERM', '-f', 'JLinkGDBServerCLExe'],
                timeout=1.0,
                check=False,
            )
            time.sleep(0.5)
            subprocess.run(
                ['pkill', '-KILL', '-f', 'JLinkGDBServerCLExe'],
                timeout=1.0,
                check=False,
            )
            time.sleep(0.2)
        except FileNotFoundError:
            pass
        return

    try:
        with open(JLINK_PIDFILE, 'r') as f:
            pid = int(f.read().strip())

        # Use pkill to kill by name - this avoids zombie issues
        # and ensures all children are terminated
        try:
            subprocess.run(
                ['pkill', '-TERM', '-f', 'JLinkGDBServerCLExe'],
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
                os.remove(JLINK_PIDFILE)
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
                ['pkill', '-KILL', '-f', 'JLinkGDBServerCLExe'],
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
            os.remove(JLINK_PIDFILE)
        except FileNotFoundError:
            pass

    except Exception as exc:
        logger.error(f'Failed to stop JLinkGDBServer: {exc}')


def start_jlink_gdbserver(device, speed='adaptive', transport='SWD', halt=False, gdb_port=2331, script_file=None):
    """Start JLinkGDBServer process.

    Args:
        device: Target device name (e.g., 'R7FA0E107')
        speed: SWD/JTAG speed in kHz or 'adaptive'
        transport: Transport protocol ('SWD' or 'JTAG')
        halt: Whether to halt the device when connecting
        gdb_port: GDB server port (default: 2331)
        script_file: Optional path to J-Link script file (.JLinkScript)

    Returns:
        dict: {'pid': int, 'status': str, 'gdb_port': int}

    Raises:
        Exception: If JLinkGDBServer not found or fails to start
    """
    jlink_exe = get_jlink_gdb_server_path()
    if not jlink_exe:
        raise Exception('JLinkGDBServerCLExe not found')

    # Ensure gdb_port is free: orphan server may hold 2331 without a PID file.
    stop_jlink_gdbserver()
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

    cmd.extend([
        '-select', 'USB',
        '-port', str(gdb_port),
        '-RTTTelnetPort', '9090',
        '-LocalhostOnly', '0',  # Allow connections from any IP (Tailscale)
        '-stayrunning', '1',    # Keep server running
        '-ir',                   # Init Registers - enables RTT
        '-nogui',
        '-logtofile',
        '-log', str(JLINK_LOGFILE)
    ])

    # Add J-Link script file if provided
    if script_file and os.path.exists(script_file):
        cmd.extend(['-JLinkScriptFile', script_file])
        logger.info(f'Using J-Link script file: {script_file}')

    logger.info(f'Starting JLinkGDBServer: {" ".join(cmd)}')

    # Start the process in the background
    with open(JLINK_LOGFILE, 'w') as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setpgrp  # Create new process group
        )

    # Write PID to file
    with open(JLINK_PIDFILE, 'w') as f:
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
            with open(JLINK_LOGFILE, 'r') as log:
                log_contents = log.read()
                if log_contents:
                    error_msg += f'\n\nJLinkGDBServer output:\n{log_contents}'
        except Exception as log_err:
            logger.warning(f'Could not read logfile: {log_err}')

        # Clean up PID file
        try:
            os.remove(JLINK_PIDFILE)
        except FileNotFoundError:
            pass

        raise Exception(error_msg)

    # Additional check: verify process exists using os.kill
    try:
        os.kill(proc.pid, 0)  # Signal 0 just checks existence
    except OSError:
        error_msg = f'JLinkGDBServer process {proc.pid} not found after startup'
        try:
            with open(JLINK_LOGFILE, 'r') as log:
                log_contents = log.read()
                if log_contents:
                    error_msg += f'\n\nJLinkGDBServer output:\n{log_contents}'
        except Exception:
            pass
        raise Exception(error_msg)

    return {
        'pid': proc.pid,
        'status': 'started',
        'gdb_port': gdb_port
    }
