# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Process management for debug tools

This module handles starting and stopping J-Link GDB server processes.
"""

import os
import logging
import subprocess
from pathlib import Path
from .mappings import (
    JL_PIDFILE,
    JL_LOGFILE,
)

logger = logging.getLogger(__name__)


def start_jlink(cmd_args):
    """
    Start J-Link GDB server process

    Args:
        cmd_args: List of command-line arguments for JLinkGDBServer

    Returns:
        subprocess.CompletedProcess result

    Raises:
        subprocess.CalledProcessError: If J-Link fails to start
    """
    logfile = JL_LOGFILE
    pidfile = JL_PIDFILE

    # Use the canonical search path (handles Linux container, third_party
    # mounts, and macOS SEGGER .pkg install location at /Applications/SEGGER).
    from .gdbserver import get_jlink_gdb_server_path
    jlink_exe = get_jlink_gdb_server_path() or 'JLinkGDBServerCLExe'

    cmd = [jlink_exe] + cmd_args + [
        '-select', 'USB',
        '-port', '2331',
        '-RTTTelnetPort', '9090',
        '-LocalhostOnly', '0',  # Allow connections from any IP (Tailscale network)
        '-stayrunning', '1',  # Keep server running even when all clients disconnect
        '-ir',  # CRITICAL: Init Registers - enables RTT control block detection
        '-nogui',
        '-logtofile',
        '-log', str(logfile)
    ]

    logger.debug(f'Starting J-Link GDB Server: {" ".join(cmd)}')

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

    logger.debug(f'J-Link GDB Server started with PID {proc.pid}')

    # Wait a moment for J-Link to initialize and write to logfile
    import time
    time.sleep(0.5)  # Reduced from 1.0s - J-Link initializes quickly

    # Create a mock CompletedProcess for compatibility
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=0,
        stdout=b'',
        stderr=b''
    )


def stop_jlink():
    """
    Stop J-Link GDB server process and all its children, and reap zombies
    """
    pidfile = JL_PIDFILE

    try:
        # Read PID from file
        if os.path.exists(pidfile):
            with open(pidfile, 'r') as f:
                pid = int(f.read().strip())

            # Kill the process and its entire process group to prevent zombies
            # J-Link spawns child processes (JLinkGUIServerE) that become zombies if not cleaned up
            try:
                import signal
                import time

                # Kill the entire process group (J-Link and all its children)
                # Since we started J-Link with os.setpgrp, it's in its own process group
                # The process group ID is the same as the PID
                try:
                    # Send SIGTERM to the entire process group
                    os.killpg(pid, signal.SIGTERM)
                    logger.debug(f'Sent SIGTERM to J-Link process group {pid}')
                except ProcessLookupError:
                    # Process group doesn't exist, try killing just the process
                    try:
                        os.kill(pid, signal.SIGTERM)
                        logger.debug(f'Sent SIGTERM to J-Link process {pid}')
                    except ProcessLookupError:
                        logger.debug(f'J-Link process {pid} not found')
                        return

                # Give processes time to exit gracefully
                time.sleep(0.5)

                # Force kill if still running
                try:
                    # Check if still alive by sending signal 0
                    os.kill(pid, 0)
                    # Still alive, force kill the process group
                    logger.debug(f'J-Link process {pid} did not exit, forcing SIGKILL')
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        os.kill(pid, signal.SIGKILL)
                    time.sleep(0.2)
                except ProcessLookupError:
                    # Process is dead
                    logger.debug(f'J-Link process {pid} exited')

                # CRITICAL: Reap zombie processes
                # After killing processes, we must call waitpid() to reap them
                # This removes the zombie entries from the process table
                # We use WNOHANG to avoid blocking, and loop to catch all children
                zombies_reaped = 0
                reap_start = time.time()
                max_reap_time = 2.0  # Maximum 2 seconds to reap zombies

                while time.time() - reap_start < max_reap_time:
                    try:
                        # Wait for any child process in the process group
                        # -pid means wait for any process in process group pid
                        # os.WNOHANG means return immediately if no child has exited
                        dead_pid, status = os.waitpid(-pid, os.WNOHANG)
                        if dead_pid == 0:
                            # No more zombie children available right now
                            break
                        zombies_reaped += 1
                        logger.debug(f'Reaped zombie process {dead_pid} (exit status: {status})')
                    except ChildProcessError:
                        # No more children to reap
                        break
                    except ProcessLookupError:
                        # Process group doesn't exist
                        break
                    except OSError as e:
                        # Other OS errors (e.g., EINVAL, ECHILD)
                        logger.debug(f'Error reaping zombies: {e}')
                        break

                if zombies_reaped > 0:
                    logger.debug(f'Reaped {zombies_reaped} zombie process(es) from J-Link process group')

                logger.debug(f'Stopped J-Link process {pid} and its children')

            except ProcessLookupError:
                logger.debug(f'J-Link process {pid} not found')
            except PermissionError:
                logger.error(f'Permission denied killing J-Link process {pid}')

            # Remove PID file
            try:
                os.remove(pidfile)
            except FileNotFoundError:
                pass
        else:
            # This is normal when J-Link isn't running - use debug level
            logger.debug(f'J-Link PID file not found: {pidfile}')

    except Exception as exc:
        logger.error(f'Failed to stop J-Link: {exc}')