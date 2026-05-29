# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Development-related CLI commands.

This package contains commands for embedded development and debugging:
- debug: Debug firmware and manage debug sessions (flash, reset, gdbserver, etc.)
- arm: Control robotic arm position and movement
- python: Run Python scripts on box hardware
- devenv: Docker-based development environment management

These commands handle debugging, robotic arm control, Python execution,
and development environment management. The debug subpackage includes
specialized service clients and caching for efficient debug operations.
"""

# Arm commands (migrated from cli/arm/)
from .arm import arm

# Python commands (migrated from cli/python/)
from .python import python, run_python_internal, run_python_internal_get_output

# Rust (pre-compiled binary) execution command
from .rust import rust

# Development environment commands (migrated from cli/devenv/)
from .devenv import devenv

# Debug commands are lazily imported to avoid circular dependencies
# Debug module imports cli/python/commands.py which imports cli/debug/tunnel.py
# which would cause circular import if loaded eagerly

__all__ = [
    # Command groups - all four exported
    "_debug",
    "arm",
    "python",
    "rust",
    "devenv",
    # Internal functions for backward compatibility
    "run_python_internal",
    "run_python_internal_get_output",
]


def __getattr__(name):
    """Lazy import for debug module to avoid circular dependencies."""
    if name == "_debug":
        from .debug import _debug
        return _debug
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
