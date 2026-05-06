# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.python - Python Script Execution Service

This module provides HTTP endpoints for executing Python scripts on the box.
It handles script upload, environment setup, and output streaming.

Migrated from gateway/controller/controller/application/views/run.py (legacy, removed)

Key features:
- Execute Python scripts in the pre-running Python container
- Stream output over HTTP with multiplexed stdout/stderr/output_channel
- Support for module uploads with automatic pip install
- Environment variable injection
- Process management (kill, detach)
- Organization secrets injection
"""

from .service import create_python_service, run_python_service
from .executor import PythonExecutor
from .exceptions import (
    PipInstallError,
    MissingModuleFolderError,
    InvalidSignalError,
    LagerPythonInvalidProcessIdError,
)

__all__ = [
    # Service
    'create_python_service',
    'run_python_service',
    # Executor
    'PythonExecutor',
    # Exceptions
    'PipInstallError',
    'MissingModuleFolderError',
    'InvalidSignalError',
    'LagerPythonInvalidProcessIdError',
]
