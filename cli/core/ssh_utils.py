# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
SSH Connection Management with ControlMaster support

This module provides SSH connection pooling using OpenSSH's ControlMaster feature,
which allows multiple SSH commands to reuse a single TCP connection, dramatically
reducing connection overhead from ~300ms to ~10ms per command.
"""
import os
import subprocess
import time
from pathlib import Path
from typing import Optional
import click


def host_in_known_hosts(ip: str) -> bool:
    """Check if a host IP exists in ~/.ssh/known_hosts.

    Used to distinguish between a genuinely new host (not yet in known_hosts)
    and a host whose key has changed (already in known_hosts but mismatched).

    Args:
        ip: IP address to look up

    Returns:
        True if the host already has an entry in known_hosts, False otherwise
    """
    known_hosts_path = Path.home() / ".ssh" / "known_hosts"
    if not known_hosts_path.exists():
        return False

    try:
        result = subprocess.run(
            ["ssh-keygen", "-F", ip],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


class SSHConnectionPool:
    """
    Manages reusable SSH connections using OpenSSH ControlMaster feature.

    This allows multiple SSH commands to reuse a single TCP connection,
    dramatically reducing connection overhead from ~300ms to ~10ms per command.
    """

    def __init__(self):
        self.control_dir = Path.home() / '.lager_cache' / 'ssh_control'
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self._active_connections = {}

    def get_control_path(self, host):
        """Get the control socket path for a given host."""
        # Sanitize host to make it filesystem-safe
        safe_host = host.replace(':', '_').replace('/', '_')
        return str(self.control_dir / f'lager-{safe_host}')

    def get_ssh_options(self, host, persist_time='10m'):
        """
        Get SSH options for connection reuse.

        Args:
            host: Target hostname/IP
            persist_time: How long to keep connection alive (default: 10m)

        Returns:
            List of SSH options to pass to ssh command
        """
        control_path = self.get_control_path(host)

        return [
            '-o', 'ControlMaster=auto',
            '-o', f'ControlPath={control_path}',
            '-o', f'ControlPersist={persist_time}',
            # Send keepalive packets every 30 seconds (reduced from 60s)
            # This helps maintain connections through firewalls/routers
            # that may drop inactive connections after 5 minutes
            '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3',
            '-o', 'ConnectTimeout=10',
        ]

    def ensure_connection(self, host, user='lagerdata', port=22):
        """
        Ensure a master connection exists for the given host.

        Args:
            host: Target hostname/IP
            user: SSH username (default: lagerdata)
            port: SSH port

        Returns:
            True if connection is active, False otherwise
        """
        control_path = self.get_control_path(host)

        # Check if connection already exists
        check_cmd = [
            'ssh', '-O', 'check',
            '-o', f'ControlPath={control_path}',
            f'{user}@{host}',
        ]

        result = subprocess.run(
            check_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        if result.returncode == 0:
            # Connection exists and is active
            return True

        # Start a new master connection
        start_cmd = [
            'ssh', '-fNM',  # -f: background, -N: no command, -M: master mode
            '-o', 'ControlMaster=yes',
            '-o', f'ControlPath={control_path}',
            '-o', 'ControlPersist=10m',
            # Send keepalive packets every 30 seconds (reduced from 60s)
            # This prevents timeouts from intermediate firewalls/routers
            '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3',
            '-p', str(port),
            f'{user}@{host}',
        ]

        try:
            subprocess.run(start_cmd, check=True, timeout=15)
            time.sleep(0.5)  # Brief delay for connection establishment
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            # Don't print error here - let the calling code handle it
            return False

    def close_connection(self, host, user='lagerdata'):
        """Close the master connection for a given host."""
        control_path = self.get_control_path(host)

        close_cmd = [
            'ssh', '-O', 'exit',
            '-o', f'ControlPath={control_path}',
            f'{user}@{host}',
        ]

        subprocess.run(
            close_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

    def close_all_connections(self):
        """Close all active SSH master connections."""
        for control_file in self.control_dir.glob('lager-*'):
            try:
                control_file.unlink()
            except Exception:
                pass


# Global connection pool instance
_ssh_pool = SSHConnectionPool()


def get_ssh_connection_pool():
    """Get the global SSH connection pool instance."""
    return _ssh_pool


def get_reusable_ssh_command(host, user='lagerdata', port=22, command=None):
    """
    Build an SSH command that uses connection reuse.

    Args:
        host: Target hostname/IP
        user: SSH username (default: lagerdata)
        port: SSH port
        command: Command to execute (if None, just establishes connection)

    Returns:
        List of command arguments suitable for subprocess.run()
    """
    pool = get_ssh_connection_pool()
    pool.ensure_connection(host, user, port)

    ssh_cmd = ['ssh'] + pool.get_ssh_options(host)
    ssh_cmd += ['-p', str(port), f'{user}@{host}']

    if command:
        if isinstance(command, list):
            ssh_cmd += command
        else:
            ssh_cmd.append(command)

    return ssh_cmd
