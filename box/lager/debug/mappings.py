# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Device and interface mappings for debug tools

This module contains utility functions for checking J-Link debugger status.
"""

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Log and PID file paths for J-Link (use /tmp for write access)
JL_LOGFILE = '/tmp/jlink.log'
JL_PIDFILE = '/tmp/jlink.pid'


def readfile(filepath):
    """
    Read file contents safely

    Args:
        filepath: Path to file

    Returns:
        File contents as string, or None if file doesn't exist
    """
    try:
        with open(filepath, 'r') as f:
            return f.read()
    except (OSError, FileNotFoundError):
        return None


def read_pidfile(pidfile, max_tries=1, interval=0):
    """
    Read PID from pidfile with retry logic

    Args:
        pidfile: Path to PID file
        max_tries: Maximum number of read attempts
        interval: Time to wait between attempts

    Returns:
        PID as integer, or None if file doesn't exist or is invalid
    """
    for _ in range(max_tries):
        try:
            with open(pidfile, 'r') as f:
                content = f.read().strip()
                if content:
                    return int(content)
        except (OSError, FileNotFoundError, ValueError):
            pass
        if interval > 0:
            time.sleep(interval)
    return None


def check_process(pid):
    """
    Check if process is running

    Args:
        pid: Process ID

    Returns:
        True if process exists, False otherwise
    """
    try:
        # Sending signal 0 checks if process exists without actually sending a signal
        import os
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def check_logfile(logfile_path, max_tries=3, mcu=None):
    """
    Check if debugger logfile indicates successful connection

    Args:
        logfile_path: Path to log file
        max_tries: Maximum number of attempts
        mcu: MCU identifier (unused, kept for compatibility)

    Returns:
        Tuple of (success: bool, logfile_contents: str)
    """
    logfile = readfile(logfile_path)
    target_port = 2331  # J-Link GDB server port

    if logfile:
        # Check for failure indicators first
        failure_indicators = [
            'ERROR: Could not connect to target',
            'Target connection failed',
            'GDBServer will be closed',
            'Could not connect to target',
        ]

        if any(indicator in logfile for indicator in failure_indicators):
            return (False, logfile)

        # Check for various success indicators
        success_indicators = [
            f'Listening on port {target_port} for gdb connections',
            'Waiting for GDB connection',  # J-Link indicator
            'gdb services need one or more targets defined',
        ]

        if any(indicator in logfile for indicator in success_indicators):
            # For J-Link, we need to make sure it's still in a good state
            # Wait a bit more to ensure no error occurs after "Listening" message
            if logfile_path == JL_LOGFILE and max_tries == 3:
                time.sleep(1.0)
                return check_logfile(logfile_path, max_tries - 1, mcu=mcu)
            return (True, logfile)

        if max_tries <= 0:
            return (False, logfile)
        time.sleep(0.5)
        return check_logfile(logfile_path, max_tries - 1, mcu=mcu)

    if max_tries <= 0:
        return (False, None)
    time.sleep(0.5)
    return check_logfile(logfile_path, max_tries - 1, mcu=mcu)


def _get_debugger_status(pidfile, logfile_path, mcu=None):
    """
    Check whether a debugger is running

    Args:
        pidfile: Path to PID file
        logfile_path: Path to log file
        mcu: MCU identifier (unused, kept for compatibility)

    Returns:
        Dictionary with keys:
            - running: bool indicating if debugger is running
            - cmdline: list of command-line arguments if running
            - logfile: contents of log file
    """
    running = False
    cmdline = None
    logfile = None

    # Use more retries since J-Link takes longer to start
    max_logfile_tries = 10

    pid = read_pidfile(pidfile, max_tries=1, interval=0)
    if pid:
        running = check_process(pid)
        if running:
            (running, logfile) = check_logfile(logfile_path, max_tries=max_logfile_tries, mcu=mcu)

            # Double-check the process is still running after logfile check
            # (it might have exited due to connection failure)
            if running and not check_process(pid):
                running = False

            if running:
                try:
                    from ..process_utils import get_process_cmdline
                    cmdline = get_process_cmdline(pid)
                except Exception:
                    pass

    if logfile is None:
        logfile = readfile(logfile_path)

    return dict(running=running, cmdline=cmdline, logfile=logfile)


def get_jlink_status():
    """
    Check whether J-Link GDB server is running

    Returns:
        Dictionary with status information
    """
    return _get_debugger_status(JL_PIDFILE, JL_LOGFILE)
