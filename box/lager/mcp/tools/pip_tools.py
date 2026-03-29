# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for managing Python packages on Lager boxes."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_pip_list(box: str) -> str:
    """List installed Python packages on a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("pip", "list", "--box", box)


@mcp.tool()
def lager_pip_install(box: str, packages: str) -> str:
    """Install Python packages on a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
        packages: Space-separated package names (e.g., 'numpy pandas')
    """
    args = ["pip", "install"] + packages.split() + ["--yes", "--box", box]
    return run_lager(*args)


@mcp.tool()
def lager_pip_uninstall(box: str, packages: str) -> str:
    """Uninstall Python packages from a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
        packages: Space-separated package names (e.g., 'numpy pandas')
    """
    args = ["pip", "uninstall"] + packages.split() + ["--yes", "--box", box]
    return run_lager(*args)


@mcp.tool()
def lager_pip_apply(box: str) -> str:
    """Install packages from the box's package list into the running container.

    Reads the package list (user_requirements.txt) from the box and installs
    all listed packages. Useful after editing the package list or after a
    container restart.

    Note: Packages installed this way are in the running container and won't
    persist after a container restart unless the box is rebuilt with
    'lager update'.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("pip", "apply", "--yes", "--box", box)
