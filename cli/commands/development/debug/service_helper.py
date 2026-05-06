# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Debug Service Helper

Utilities for detecting and communicating with the persistent debug service.
"""
import socket
import subprocess
import time
import requests
import atexit
from typing import Optional, Dict

# Global tunnel cache - persist across command invocations
_tunnel_cache: Dict[str, 'ServiceTunnel'] = {}


class ServiceTunnel:
    """Manages SSH tunnel to debug service."""

    def __init__(self, box_ip: str, username: str = 'lagerdata', local_port: int = 8765, remote_port: int = 8765):
        """
        Initialize ServiceTunnel.

        Args:
            box_ip: IP address of the box
            username: SSH username (defaults to 'lagerdata')
            local_port: Local port for tunnel (default 8765)
            remote_port: Remote port on box (default 8765)
        """
        self.box_ip = box_ip
        self.username = username
        self.local_port = local_port
        self.remote_port = remote_port
        self.tunnel_process = None

    def is_tunnel_active(self) -> bool:
        """Check if SSH tunnel is already established."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', self.local_port))
            sock.close()
            return result == 0
        except:
            return False

    def establish_tunnel(self) -> bool:
        """Establish SSH tunnel to debug service."""
        from ....core.ssh_utils import get_ssh_connection_pool

        pool = get_ssh_connection_pool()
        pool.ensure_connection(self.box_ip, user=self.username)

        # Build SSH tunnel command
        ssh_opts = pool.get_ssh_options(self.box_ip)
        tunnel_cmd = [
            'ssh',
            '-N',  # No command execution
            '-L', f'{self.local_port}:127.0.0.1:{self.remote_port}',  # Local port forwarding
        ] + ssh_opts + [f'{self.username}@{self.box_ip}']

        try:
            # Start tunnel in background
            self.tunnel_process = subprocess.Popen(
                tunnel_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Wait for tunnel to establish
            for _ in range(10):  # Try for up to 1 second
                time.sleep(0.1)
                if self.is_tunnel_active():
                    return True

            return False

        except Exception:
            return False

    def close_tunnel(self):
        """Close SSH tunnel."""
        if self.tunnel_process:
            try:
                self.tunnel_process.terminate()
                self.tunnel_process.wait(timeout=2)
            except:
                try:
                    self.tunnel_process.kill()
                except:
                    pass
            self.tunnel_process = None


def is_service_available(box_ip: str, username: str = 'lagerdata') -> bool:
    """
    Check if debug service is available on box.

    Args:
        box_ip: Lagerbox IP address
        username: SSH username (defaults to 'lagerdata')

    Returns:
        True if service is running and reachable
    """
    tunnel = ServiceTunnel(box_ip, username=username)

    # Check if tunnel already exists
    if not tunnel.is_tunnel_active():
        # Try to establish tunnel
        if not tunnel.establish_tunnel():
            return False

    # Verify service is responding
    try:
        response = requests.get('http://127.0.0.1:8765/health', timeout=2)
        return response.status_code == 200
    except:
        return False


def ensure_service_tunnel(box_ip: str, username: str = 'lagerdata') -> Optional[ServiceTunnel]:
    """
    Ensure SSH tunnel to debug service is established.
    Uses a global cache to reuse tunnels across command invocations.

    Args:
        box_ip: Lagerbox IP address
        username: SSH username (defaults to 'lagerdata')

    Returns:
        ServiceTunnel instance if successful, None otherwise
    """
    # Create cache key that includes username
    cache_key = f"{box_ip}:{username}"

    # Check if we have a cached tunnel for this box+username
    if cache_key in _tunnel_cache:
        tunnel = _tunnel_cache[cache_key]
        if tunnel.is_tunnel_active():
            # Existing tunnel is still alive
            return tunnel
        else:
            # Tunnel died, remove from cache
            del _tunnel_cache[cache_key]

    # Create new tunnel
    tunnel = ServiceTunnel(box_ip, username=username)

    if tunnel.is_tunnel_active():
        # Tunnel already exists (from another process)
        _tunnel_cache[cache_key] = tunnel
        return tunnel

    if tunnel.establish_tunnel():
        # Cache the new tunnel
        _tunnel_cache[cache_key] = tunnel
        return tunnel

    return None


def cleanup_tunnels():
    """Clean up all tunnels on exit."""
    for tunnel in _tunnel_cache.values():
        try:
            tunnel.close_tunnel()
        except:
            pass
    _tunnel_cache.clear()


# Register cleanup function
atexit.register(cleanup_tunnels)
