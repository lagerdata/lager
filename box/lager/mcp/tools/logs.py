# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for managing logs on Lager boxes."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_logs_clean(box: str, older_than: str = "1d") -> str:
    """Clean old log files from a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
        older_than: Delete logs older than this duration (default: '1d').
            Examples: '1d', '12h', '7d'
    """
    return run_lager(
        "logs", "clean",
        "--older-than", older_than,
        "--yes", "--box", box,
    )


@mcp.tool()
def lager_logs_size(box: str = "") -> str:
    """Show the total size of log files on a Lager box.

    Args:
        box: Box name (e.g., 'DEMO'). Leave empty for default box.
    """
    if box:
        return run_lager("logs", "size", "--box", box)
    return run_lager("logs", "size")


@mcp.tool()
def lager_logs_docker(box: str, container: str = "") -> str:
    """Show Docker container logs from a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
        container: Container name (omit for default container)
    """
    args = ["logs", "docker", "--box", box]
    if container:
        args.extend(["--container", container])
    return run_lager(*args)
