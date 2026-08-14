# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
SSH Connection Management with ControlMaster support

This module provides SSH connection pooling using OpenSSH's ControlMaster feature,
which allows multiple SSH commands to reuse a single TCP connection, dramatically
reducing connection overhead from ~300ms to ~10ms per command.
"""
import os
import subprocess
import tempfile
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


# Sentinel for ensure_connection's `identity_file`: "work it out" — offer the
# lager_box key when it exists, falling back to ssh's default identities if the
# box rejects it. Distinct from an explicit None, which means "this caller has
# already decided: offer no -i at all".
_AUTO_IDENTITY = object()


def _box_ssh():
    """Deferred handle on the lager_box key primitives.

    cli.core sits *below* cli.commands in the import graph — every command
    module imports from here — so this cannot be a module-level import
    without closing a cycle. Deferring it keeps one definition of the key
    path and the auth-failure markers instead of a second copy drifting
    down here.
    """
    from ..commands.box import _ssh
    return _ssh


def _auto_identity_candidates():
    """Identities to try for a master connection, best first.

    ``[lager_box, None]`` when the key exists: offer it, and fall back to
    ssh's defaults if the box rejects it (``-i`` replaces the default
    identity list rather than adding to it, so a box authorized only by the
    operator's own key would otherwise be locked out). ``[None]`` when
    there is no key to offer.
    """
    try:
        key = _box_ssh().lager_box_key_if_present()
    except ImportError:
        return [None]
    return [key, None] if key else [None]


def _is_auth_failure(stderr):
    """True when ssh's stderr says the server rejected our credentials."""
    try:
        return _box_ssh().is_auth_failure(stderr)
    except ImportError:
        return False


class SSHConnectionPool:
    """
    Manages reusable SSH connections using OpenSSH ControlMaster feature.

    This allows multiple SSH commands to reuse a single TCP connection,
    dramatically reducing connection overhead from ~300ms to ~10ms per command.

    The pool also remembers which identity authenticated to each host, so
    :meth:`get_ssh_options` can re-offer it. That matters because the master
    is not immortal: ControlMaster=auto silently opens a *fresh* connection
    when the socket is gone (ControlPersist expiry, a long command that closed
    it, a reboot), and that connection has to authenticate for real. Without
    the identity it falls back on whatever ``~/.ssh/config`` names — the exact
    dependency that leaves lager commands failing on a box where the lager_box
    key is the only thing actually authorized.
    """

    def __init__(self):
        self.control_dir = Path.home() / '.lager_cache' / 'ssh_control'
        self.control_dir.mkdir(parents=True, exist_ok=True)
        # host -> identity path that authenticated (or None for "ssh defaults").
        # Populated by ensure_connection; absent until it has run for a host.
        self._identities = {}

    def get_control_path(self, host):
        """Get the control socket path for a given host."""
        # Sanitize host to make it filesystem-safe
        safe_host = host.replace(':', '_').replace('/', '_')
        return str(self.control_dir / f'lager-{safe_host}')

    def identity_for(self, host):
        """Identity that authenticated to `host`, or None.

        None both before :meth:`ensure_connection` has run for the host and
        when it authenticated with ssh's own default identities.
        """
        return self._identities.get(host)

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

        opts = []
        # Re-offer whatever authenticated the master, for the case where this
        # command has to open its own connection instead of riding the socket.
        identity = self.identity_for(host)
        if identity:
            opts.extend(['-i', identity])

        opts.extend([
            '-o', 'ControlMaster=auto',
            '-o', f'ControlPath={control_path}',
            '-o', f'ControlPersist={persist_time}',
            # Send keepalive packets every 30 seconds (reduced from 60s)
            # This helps maintain connections through firewalls/routers
            # that may drop inactive connections after 5 minutes
            '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3',
            '-o', 'ConnectTimeout=10',
        ])
        return opts

    def ensure_connection(self, host, user='lagerdata', port=22,
                          identity_file=_AUTO_IDENTITY):
        """
        Ensure a master connection exists for the given host.

        Args:
            host: Target hostname/IP
            user: SSH username (default: lagerdata)
            port: SSH port
            identity_file: Identity to offer. Defaults to working it out —
                the lager_box key when it exists, retried without it if the
                box rejects the key. Pass an explicit path (or None for "no
                -i") when the caller has already probed and knows the answer.

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

        def start_master(identity):
            """Start the master, returning (returncode, stderr)."""
            # -f: background, -N: no command, -M: master mode
            start_cmd = ['ssh', '-fNM']
            if identity:
                start_cmd.extend(['-i', identity])
            start_cmd.extend([
                '-o', 'ControlMaster=yes',
                '-o', f'ControlPath={control_path}',
                '-o', 'ControlPersist=10m',
                # Send keepalive packets every 30 seconds (reduced from 60s)
                # This prevents timeouts from intermediate firewalls/routers
                '-o', 'ServerAliveInterval=30',
                '-o', 'ServerAliveCountMax=3',
                '-p', str(port),
                f'{user}@{host}',
            ])
            # A real file for stderr, NOT capture_output/PIPE. `-f` daemonizes
            # without closing its inherited descriptors, so the master holds
            # the write end of a pipe open for its whole ControlPersist life
            # and subprocess.run would block reading it until the timeout —
            # turning every successful master start into a 15s hang and a
            # False return. A file descriptor the master keeps open costs
            # nothing. Auth failures are written before `-f` forks (it
            # daemonizes only after authenticating), so they are all here by
            # the time run() returns.
            with tempfile.TemporaryFile(mode='w+') as errf:
                proc = subprocess.run(
                    start_cmd, stdout=subprocess.DEVNULL, stderr=errf, timeout=15,
                )
                errf.seek(0)
                return proc.returncode, errf.read()

        if identity_file is _AUTO_IDENTITY:
            candidates = _auto_identity_candidates()
        else:
            candidates = [identity_file]

        try:
            for attempt, identity in enumerate(candidates):
                returncode, stderr = start_master(identity)
                if returncode == 0:
                    self._identities[host] = identity
                    time.sleep(0.5)  # Brief delay for connection establishment
                    return True
                # Only an auth rejection is worth another connection; a
                # timeout or unreachable box fails the same way twice.
                if attempt + 1 < len(candidates) and not _is_auth_failure(stderr):
                    break
            return False
        except (subprocess.SubprocessError, OSError):
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
