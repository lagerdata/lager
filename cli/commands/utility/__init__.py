# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Utility CLI commands.

This package contains utility commands for managing Lager configuration:
- defaults: Manage default settings (box, nets, serial port)
- binaries: Manage custom binaries on boxes
- update: Update box code from GitHub repository
- exec_: Execute commands in local Docker container (devenv)
- logs: Manage and inspect box logs
- webcam: Webcam streaming management

Pip package management lives under `lager box-config pip` (declarative,
applied alongside mounts/volumes/env via `lager box-config apply`).
"""

# Import all commands from local files (migrated from original locations)
from .defaults import defaults
from .binaries import binaries
from .update import update
from .exec_ import exec_
from .logs import logs
from .webcam import webcam
from .install import install
from .uninstall import uninstall
from .install_wheel import install_wheel
from .login import login, logout, whoami

__all__ = [
    "defaults",
    "binaries",
    "update",
    "exec_",
    "logs",
    "webcam",
    "install",
    "uninstall",
    "install_wheel",
    "login",
    "logout",
    "whoami",
]
