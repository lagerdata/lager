# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Box state directory resolution.

The Lager box keeps its mutable state (saved nets, lock file, version, control
plane config, secrets, etc.) in a single directory. On Linux Cybergeek boxes
this is /etc/lager (bind-mounted into the Docker container). On a native macOS
box it lives under /Library/Application Support/Lager and is owned by the
dedicated lagerdata user.

The LAGER_STATE_DIR environment variable overrides the platform default and is
how the macOS launchd plist injects the path into the box services.
"""

import os
import sys
from pathlib import Path


_LINUX_DEFAULT = Path("/etc/lager")
_MACOS_DEFAULT = Path("/Library/Application Support/Lager")


def get_state_dir() -> Path:
    """Return the box state directory for the current platform."""
    override = os.environ.get("LAGER_STATE_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return _MACOS_DEFAULT
    return _LINUX_DEFAULT
