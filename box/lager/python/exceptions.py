# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager.python.exceptions - Python Execution Exceptions

Exception classes for Python script execution errors.

Migrated from gateway/controller/controller/application/exceptions.py (legacy, removed)
"""


class PythonExecutionError(Exception):
    """Base exception for Python execution errors"""
    pass


class PipInstallError(PythonExecutionError):
    """Raised when pip install fails"""
    def __init__(self, output):
        self.output = output
        super().__init__(f"Pip install failed: {output.decode() if isinstance(output, bytes) else output}")


class MissingModuleFolderError(PythonExecutionError):
    """Raised when module folder is missing"""
    def __init__(self):
        super().__init__("Could not find module folder")


class InvalidSignalError(PythonExecutionError):
    """Raised when an invalid signal number is provided"""
    def __init__(self, signal):
        self.signal = signal
        super().__init__(f"Invalid signal: {signal}")


class LagerPythonInvalidProcessIdError(PythonExecutionError):
    """Raised when an invalid process ID (UUID) is provided"""
    def __init__(self, process_id):
        self.process_id = process_id
        super().__init__(f"Invalid process UUID: {process_id}")


class LagerPythonProcessNotFoundError(PythonExecutionError):
    """Raised when a detached process cannot be found for reattach"""
    def __init__(self, process_id):
        self.process_id = process_id
        super().__init__(f"Process not found: {process_id}")
