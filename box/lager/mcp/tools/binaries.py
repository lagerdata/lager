# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for managing firmware binaries on Lager boxes."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_binaries_list(box: str) -> str:
    """List firmware binaries stored on a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("binaries", "list", "--box", box)


@mcp.tool()
def lager_binaries_add(box: str, file_path: str, name: str = "") -> str:
    """Upload a firmware binary to a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
        file_path: Local path to the binary file to upload
        name: Name to store the binary as (omit to use filename)
    """
    args = ["binaries", "add", file_path, "--yes", "--box", box]
    if name:
        args.extend(["--name", name])
    return run_lager(*args)


@mcp.tool()
def lager_binaries_remove(box: str, name: str) -> str:
    """Remove a firmware binary from a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
        name: Name of the binary to remove
    """
    return run_lager("binaries", "remove", name, "--yes", "--box", box)
