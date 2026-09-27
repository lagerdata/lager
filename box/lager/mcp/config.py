# Copyright 2024-2026 Lager Data
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

BOX_ID_PATH = "/etc/lager/box_id"
#: The host's hostname, bind-mounted into the container. The container's own
#: /etc/hostname is the container id, so it is never used as a fallback.
HOST_HOSTNAME_PATH = "/host/etc/hostname"


def _read_line(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def get_box_id(
    *, box_id_path: str = BOX_ID_PATH, hostname_path: str = HOST_HOSTNAME_PATH,
) -> str:
    """The box identifier: the box_id file, else ``LAGER_BOX_ID``, else the
    host's hostname, else ``"unknown"``.

    A box installed before the id file existed has no ``/etc/lager/box_id``
    and no env value, but its hostname is the name every operator knows it
    by; answering ``unknown`` there left every tool reply and the manifest
    without an identity a fleet client could key on.
    """
    return (
        _read_line(box_id_path)
        or os.environ.get("LAGER_BOX_ID", "").strip()
        or _read_line(hostname_path)
        or "unknown"
    )


def get_box_version() -> str:
    """Read the box software version from /etc/lager/version."""
    try:
        with open("/etc/lager/version", "r") as fh:
            content = fh.read().strip()
            return content.split("|", 1)[0] if "|" in content else content
    except FileNotFoundError:
        return "unknown"


def control_tools_enabled() -> bool:
    """Whether the scoped box-control tools are exposed (opt-in, default off).

    The MCP server is read-only by default. The control tools (probe status,
    net status, hub power-cycle) mutate or probe box state, so they are gated
    behind ``LAGER_MCP_ALLOW_CONTROL`` and only registered when an operator
    explicitly opts in. Truthy values are anything other than the usual
    off-spellings, matching the env-flag style used elsewhere on the box
    (e.g. ``lager.debug.jlink``).
    """
    return os.environ.get("LAGER_MCP_ALLOW_CONTROL", "0").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )


def exec_tools_enabled() -> bool:
    """Whether the general box-control PRIMITIVES are exposed (opt-in, default off).

    These tools (``box_exec`` arbitrary command execution, ``read_file`` /
    ``write_file`` / ``list_dir``) let an MCP client make arbitrary changes to
    the box's test environment — far more powerful than the scoped control
    helpers — so they sit behind their OWN flag, ``LAGER_MCP_ALLOW_EXEC``,
    separate from ``LAGER_MCP_ALLOW_CONTROL``. An operator must enable this
    dangerous tier deliberately. Same off-spelling parse as
    ``control_tools_enabled``.
    """
    return os.environ.get("LAGER_MCP_ALLOW_EXEC", "0").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )
