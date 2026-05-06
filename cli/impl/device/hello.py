# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import sys
import socket
import subprocess

# ANSI color codes
GREEN = '\033[92m'
RESET = '\033[0m'

def get_host_hostname():
    """Get the hostname of the host machine (not the container)"""
    # Try to read hostname from mounted host filesystem
    possible_paths = [
        '/host/etc/hostname',
        '/rootfs/etc/hostname',
        '/etc/host_hostname'
    ]

    for path in possible_paths:
        try:
            with open(path, 'r') as f:
                hostname = f.read().strip()
                if hostname:
                    return hostname
        except (FileNotFoundError, PermissionError):
            continue

    # Try using hostname command directly (might work if not in strict container)
    try:
        result = subprocess.run(
            ['hostname', '-f'],
            capture_output=True,
            text=True,
            timeout=2
        )
        if result.returncode == 0:
            hostname = result.stdout.strip()
            # Don't use it if it's a container ID (hex string ~12 chars)
            if hostname and not (len(hostname) == 12 and all(c in '0123456789abcdef' for c in hostname)):
                return hostname
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback to container hostname
    return socket.gethostname()

# Get the IP address from command line argument if provided
if len(sys.argv) > 1:
    box_ip = sys.argv[1]
    hostname = get_host_hostname()
    print(f'{GREEN}Hello from {hostname} ({box_ip})!{RESET}')
else:
    print(f'{GREEN}Hello, world! Your Lager Box is connected.{RESET}')
