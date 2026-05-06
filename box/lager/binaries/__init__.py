# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.binaries - Custom binary execution helpers

This module provides utilities for running custom binaries that have been
uploaded to the box via `lager binaries add`.

Usage:
    from lager.binaries import run_custom_binary

    # Run a custom binary with arguments
    result = run_custom_binary('rt_newtmgr', 'image', 'list')

    # Check the result
    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"Error: {result.stderr}")

    # With timeout
    result = run_custom_binary('my_tool', '--verbose', timeout=60)
"""

from .runner import run_custom_binary, get_binary_path, list_binaries, BinaryNotFoundError

__all__ = ['run_custom_binary', 'get_binary_path', 'list_binaries', 'BinaryNotFoundError']
