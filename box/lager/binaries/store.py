# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.binaries.store - Disk logic behind the binaries + download-file endpoints.

Shared between the :5000 python-exec service (box/lager/python/service.py)
and the :9000 box HTTP server (box/lager/http_handlers/binaries_handler.py),
following the lock_state precedent: both servers shim the same code so the
wire contracts stay identical.

Uploaded binaries live in a directory mounted into the python container:
host ``/home/lagerdata/third_party/customer-binaries`` ==
container ``/home/www-data/customer-binaries``. Whichever server handles a
request writes to the container path when it exists (in-container) and the
host path otherwise (dev mode / host-side service).

User errors raise :class:`StoreError` carrying the HTTP status the caller
should return, so both servers map failures the same way.
"""

import os
import stat
from typing import List, Tuple

# Host path (where files are stored on the box filesystem)
HOST_BINARIES_DIR = '/home/lagerdata/third_party/customer-binaries'
# Container path (where files are mounted inside the Docker container)
CONTAINER_BINARIES_DIR = '/home/www-data/customer-binaries'

# Directories from which files may be downloaded (/download-file).
# Paths outside this allowlist are rejected regardless of traversal techniques.
ALLOWED_DOWNLOAD_ROOTS = (
    '/tmp/lager-output',
    '/tmp/lager-results',
    '/tmp/lager-job-output',
)


class StoreError(Exception):
    """A request failure with the HTTP status the endpoint should return."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _validate_name(name: str) -> None:
    if '/' in name or '\\' in name or '..' in name:
        raise StoreError(400, 'Invalid binary name')


def binaries_dir() -> str:
    """The directory writes should target for this process's context."""
    if os.path.exists(CONTAINER_BINARIES_DIR):
        return CONTAINER_BINARIES_DIR
    return HOST_BINARIES_DIR


def list_state() -> dict:
    """State for GET /binaries/list (same JSON both servers return)."""
    binaries = []
    check_path = binaries_dir()

    if os.path.exists(check_path) and os.path.isdir(check_path):
        for name in os.listdir(check_path):
            file_path = os.path.join(check_path, name)
            if os.path.isfile(file_path):
                file_stat = os.stat(file_path)
                binaries.append({
                    'name': name,
                    'size': file_stat.st_size,
                    'executable': bool(file_stat.st_mode & stat.S_IXUSR),
                })

    mounted = (os.path.exists(CONTAINER_BINARIES_DIR)
               and os.path.isdir(CONTAINER_BINARIES_DIR))
    return {
        'binaries': binaries,
        'host_path': HOST_BINARIES_DIR,
        'container_path': CONTAINER_BINARIES_DIR,
        'mounted': mounted,
    }


def add_binary(name: str, content: bytes) -> dict:
    """Write *content* as an executable binary named *name*.

    Returns the POST /binaries/add response body.
    """
    if not name:
        raise StoreError(400, 'name is required')
    _validate_name(name)

    target_dir = binaries_dir()
    os.makedirs(target_dir, exist_ok=True)

    binary_path = os.path.join(target_dir, name)
    with open(binary_path, 'wb') as f:
        f.write(content)
    os.chmod(binary_path,
             os.stat(binary_path).st_mode
             | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # If the container mount isn't active, the python container can't see the
    # new binary until it restarts.
    restart_required = not os.path.exists(CONTAINER_BINARIES_DIR)

    return {
        'success': True,
        'name': name,
        'path': os.path.join(CONTAINER_BINARIES_DIR, name),
        'size': len(content),
        'restart_required': restart_required,
    }


def remove_binary(name: str) -> dict:
    """Delete the named binary. Returns the POST /binaries/remove body."""
    if not name:
        raise StoreError(400, 'name is required')
    _validate_name(name)

    binary_path = os.path.join(binaries_dir(), name)
    if not os.path.exists(binary_path):
        raise StoreError(404, f"Binary '{name}' not found")

    os.remove(binary_path)
    return {'success': True, 'name': name}


def resolve_download_path(filename: str) -> Tuple[str, int]:
    """Validate a /download-file target; return (abs_path, size).

    Raises StoreError(403) outside the allowlist, (404) if missing, and
    (400) for non-file paths.
    """
    # Resolve to an absolute path first — eliminates all traversal sequences
    # including '..' components and absolute paths at sensitive locations.
    abs_filename = os.path.abspath(filename)

    # Allowlist check: the resolved path must reside under a permitted root.
    # The os.sep suffix prevents /tmp/lager-output-evil matching /tmp/lager-output.
    if not any(
        abs_filename == root or abs_filename.startswith(root + os.sep)
        for root in ALLOWED_DOWNLOAD_ROOTS
    ):
        raise StoreError(403, 'Access to this path is not permitted')

    if not os.path.exists(abs_filename):
        raise StoreError(404, f'File not found: {filename}')

    if not os.path.isfile(abs_filename):
        raise StoreError(400, 'Path is not a file')

    return abs_filename, os.path.getsize(abs_filename)
