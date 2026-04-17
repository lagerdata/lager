# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager.exec - Docker Container Execution Management

This module provides utilities for executing commands in Docker containers,
managing processes, and handling container lifecycle operations.

Migrated from gateway/controller/controller/application/views/run.py (legacy, removed)
"""

import sys as _sys

# Docker container operations are only available on Linux where the box runs
# inside a Docker container. On macOS (native host mode) there is no container,
# so we skip importing the Docker module entirely and expose no-op stubs.
if not _sys.platform == "darwin":
    from .docker import (
        execute_in_container,
        kill_container_process,
        is_container_running,
        get_container_ip,
        get_container_pid,
    )
else:
    def execute_in_container(*a, **kw):
        raise RuntimeError("Docker container operations are not available on the native macOS box")
    kill_container_process = execute_in_container
    def is_container_running(*a, **kw):
        return False
    def get_container_ip(*a, **kw):
        return None
    def get_container_pid(*a, **kw):
        return None

from .process import (
    stream_process_output,
    terminate_process,
    make_output_channel,
    cleanup_functions,
)

__all__ = [
    # Docker operations (no-ops on macOS)
    'execute_in_container',
    'kill_container_process',
    'is_container_running',
    'get_container_ip',
    'get_container_pid',
    # Process management
    'stream_process_output',
    'terminate_process',
    'make_output_channel',
    'cleanup_functions',
]
