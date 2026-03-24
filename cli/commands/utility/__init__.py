# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Utility CLI commands.

This package contains utility commands for managing Lager configuration:
- defaults: Manage default settings (box, nets, serial port)
- binaries: Manage custom binaries on boxes
- update: Update box code from GitHub repository
- pip: Manage pip packages in the python container
- exec_: Execute commands in local Docker container (devenv)
- logs: Manage and inspect box logs
- webcam: Webcam streaming management
"""

# Import all commands from local files (migrated from original locations)
from .defaults import defaults
from .binaries import binaries
from .update import update
from .pip import pip
from .exec_ import exec_
from .logs import logs
from .webcam import webcam
from .install import install
from .uninstall import uninstall
from .install_wheel import install_wheel

__all__ = [
    "defaults",
    "binaries",
    "update",
    "pip",
    "exec_",
    "logs",
    "webcam",
    "install",
    "uninstall",
    "install_wheel",
]
