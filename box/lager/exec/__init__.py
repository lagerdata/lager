# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.exec - Docker Container Execution Management

This module provides utilities for executing commands in Docker containers,
managing processes, and handling container lifecycle operations.

Migrated from gateway/controller/controller/application/views/run.py (legacy, removed)
"""

from .docker import (
    execute_in_container,
    kill_container_process,
    is_container_running,
    get_container_ip,
    get_container_pid,
)

from .process import (
    stream_process_output,
    terminate_process,
    make_output_channel,
    cleanup_functions,
)

__all__ = [
    # Docker operations
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
