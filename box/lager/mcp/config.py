# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
MCP server configuration for on-box deployment.

When running on the box, the server has direct access to hardware via
the lager.Net API. No remote box resolution is needed.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

MCP_PORT = int(os.environ.get("LAGER_MCP_PORT", "8100"))

# Ports for co-located box services (used for inter-service calls on localhost)
BOX_SERVICE_PORT = 5000
BOX_HARDWARE_PORT = 8080
BOX_HTTP_PORT = 9000
BOX_DEBUG_PORT = 8765


def get_box_id() -> str:
    """Read the box identifier from /etc/lager/box_id."""
    try:
        with open("/etc/lager/box_id", "r") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return os.environ.get("LAGER_BOX_ID", "unknown")


def get_box_version() -> str:
    """Read the box software version from /etc/lager/version."""
    try:
        with open("/etc/lager/version", "r") as fh:
            content = fh.read().strip()
            return content.split("|", 1)[0] if "|" in content else content
    except FileNotFoundError:
        return "unknown"
