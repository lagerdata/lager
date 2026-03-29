# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
MCP server configuration.

Resolves which Lager box this server instance is scoped to and
how to reach it (direct HTTP to box IP).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Default port for the box Python service
BOX_SERVICE_PORT = 5000
BOX_HARDWARE_PORT = 8080
BOX_HTTP_PORT = 9000


def resolve_box_ip() -> str:
    """
    Determine the target box IP for this MCP server instance.

    Resolution order:
    1. LAGER_BOX_IP environment variable (explicit override)
    2. LAGER_BOX environment variable (box name -> look up IP)
    3. Default box from ~/.lager config
    """
    # Explicit IP override
    box_ip = os.environ.get("LAGER_BOX_IP", "").strip()
    if box_ip:
        return box_ip

    # Box name -> resolve to IP via box storage
    box_name = os.environ.get("LAGER_BOX", "").strip()
    if box_name:
        try:
            from ..box_storage import get_box_ip
            ip = get_box_ip(box_name)
            if ip:
                return ip
        except Exception as exc:
            logger.warning("Could not resolve box name %r: %s", box_name, exc)

    # Fall back to default gateway from config
    try:
        from ..config import read_config_file
        cfg = read_config_file()
        gw = cfg.get("LAGER", {}).get("gateway_id", "")
        if gw:
            try:
                from ..box_storage import get_box_ip
                ip = get_box_ip(gw)
                if ip:
                    return ip
            except Exception:
                pass
    except Exception:
        pass

    return ""


def resolve_box_name() -> str:
    """Return the human-friendly box name, if known."""
    name = os.environ.get("LAGER_BOX", "").strip()
    if name:
        return name
    try:
        from ..config import read_config_file
        cfg = read_config_file()
        return cfg.get("LAGER", {}).get("gateway_id", "")
    except Exception:
        return ""
