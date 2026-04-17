# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager.binaries.runner - Run custom binaries on the box

This module provides helpers for executing custom binaries that customers
have uploaded to the box. These binaries are stored in a mounted
directory and can be called via subprocess.

The binaries are mounted at /home/www-data/customer-binaries/ inside
the container, mapped from /home/lagerdata/third_party/customer-binaries/
on the host.
"""

import os
import subprocess
from typing import List, Optional, Union


# Path where customer binaries live. On Linux this is inside the Docker
# container; on macOS it's under the LAGER_STATE_DIR.
import sys as _sys
if _sys.platform == 'darwin':
    from ..state_dir import get_state_dir as _get_state_dir
    CUSTOMER_BINARIES_PATH = str(_get_state_dir() / 'customer-binaries')
else:
    CUSTOMER_BINARIES_PATH = '/home/www-data/customer-binaries'


class BinaryNotFoundError(Exception):
    """Raised when a requested binary is not found."""
    pass


def get_binary_path(binary_name: str) -> str:
    """
    Get the full path to a custom binary.

    Args:
        binary_name: Name of the binary (without path)

    Returns:
        Full path to the binary

    Raises:
        BinaryNotFoundError: If the binary doesn't exist
    """
    # Validate name (no path traversal)
    if '/' in binary_name or '\\' in binary_name or '..' in binary_name:
        raise BinaryNotFoundError(f"Invalid binary name: {binary_name}")

    binary_path = os.path.join(CUSTOMER_BINARIES_PATH, binary_name)

    if not os.path.exists(binary_path):
        available = list_binaries()
        if available:
            raise BinaryNotFoundError(
                f"Binary '{binary_name}' not found. "
                f"Available binaries: {', '.join(available)}"
            )
        else:
            raise BinaryNotFoundError(
                f"Binary '{binary_name}' not found. "
                f"No custom binaries are installed. "
                f"Use 'lager binaries add <file> --box <box>' to upload a binary."
            )

    if not os.access(binary_path, os.X_OK):
        raise BinaryNotFoundError(
            f"Binary '{binary_name}' exists but is not executable. "
            f"Try removing and re-adding it with 'lager binaries add'."
        )

    return binary_path


def list_binaries() -> List[str]:
    """
    List all available custom binaries.

    Returns:
        List of binary names
    """
    if not os.path.exists(CUSTOMER_BINARIES_PATH):
        return []

    binaries = []
    for name in os.listdir(CUSTOMER_BINARIES_PATH):
        path = os.path.join(CUSTOMER_BINARIES_PATH, name)
        if os.path.isfile(path):
            binaries.append(name)

    return sorted(binaries)


def run_custom_binary(
    binary_name: str,
    *args: str,
    timeout: Optional[int] = 30,
    capture_output: bool = True,
    text: bool = True,
    check: bool = False,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    input: Optional[Union[str, bytes]] = None,
) -> subprocess.CompletedProcess:
    """
    Run a custom binary that was uploaded via `lager binaries add`.

    This is a convenience wrapper around subprocess.run() that handles
    finding the binary path and provides sensible defaults.

    Args:
        binary_name: Name of the binary to run (e.g., 'rt_newtmgr')
        *args: Arguments to pass to the binary
        timeout: Maximum time in seconds to wait (default: 30, None for no timeout)
        capture_output: Capture stdout/stderr (default: True)
        text: Return stdout/stderr as strings instead of bytes (default: True)
        check: Raise CalledProcessError if return code is non-zero (default: False)
        cwd: Working directory for the process (default: None)
        env: Environment variables (default: inherits from parent)
        input: Input to send to stdin (default: None)

    Returns:
        subprocess.CompletedProcess with returncode, stdout, stderr attributes

    Raises:
        BinaryNotFoundError: If the binary doesn't exist or isn't executable
        subprocess.TimeoutExpired: If the process times out
        subprocess.CalledProcessError: If check=True and return code is non-zero

    Example:
        from lager.binaries import run_custom_binary

        # Simple usage
        result = run_custom_binary('rt_newtmgr', 'image', 'list')
        print(result.stdout)

        # With error checking
        result = run_custom_binary('my_tool', '--config', 'test.cfg', check=True)

        # With custom timeout
        result = run_custom_binary('slow_tool', timeout=120)

        # Check return code
        if result.returncode != 0:
            print(f"Error: {result.stderr}")
    """
    binary_path = get_binary_path(binary_name)

    # Build command
    cmd = [binary_path] + list(args)

    # Merge environment if provided
    process_env = None
    if env is not None:
        process_env = os.environ.copy()
        process_env.update(env)

    return subprocess.run(
        cmd,
        timeout=timeout,
        capture_output=capture_output,
        text=text,
        check=check,
        cwd=cwd,
        env=process_env,
        input=input,
    )
