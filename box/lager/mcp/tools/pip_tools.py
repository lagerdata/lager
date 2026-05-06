# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for managing Python packages on Lager boxes.

Tools delegate to `lager box config pip ...` (the consolidated declarative
interface). `install` and `uninstall` chain an `apply` so the running
container reflects the change.
"""

from ..server import mcp, run_lager


@mcp.tool()
def lager_pip_list(box: str) -> str:
    """List user-installed Python packages on a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("box", "config", "pip", "list", "--box", box)


@mcp.tool()
def lager_pip_install(box: str, packages: str) -> str:
    """Install Python packages on a Lager box.

    Adds each package to the box's declarative config and applies the change
    (which restarts the lager container and runs pip install inside it).

    Args:
        box: Box name (e.g., 'DEMO')
        packages: Space-separated package names (e.g., 'numpy pandas')
    """
    add_args = ["box", "config", "pip", "add"] + packages.split() + ["--box", box]
    add_out = run_lager(*add_args)
    apply_out = run_lager("box", "config", "apply", "--yes", "--box", box)
    return add_out + "\n" + apply_out


@mcp.tool()
def lager_pip_uninstall(box: str, packages: str) -> str:
    """Uninstall Python packages from a Lager box.

    Removes each package from the box's declarative config and applies the
    change (container restart drops the package from the running container).

    Args:
        box: Box name (e.g., 'DEMO')
        packages: Space-separated package names (e.g., 'numpy pandas')
    """
    remove_args = ["box", "config", "pip", "remove"] + packages.split() + ["--box", box]
    remove_out = run_lager(*remove_args)
    apply_out = run_lager("box", "config", "apply", "--yes", "--box", box)
    return remove_out + "\n" + apply_out


@mcp.tool()
def lager_pip_apply(box: str) -> str:
    """Apply the box's declarative config (mounts, volumes, env, pip packages).

    Restarts the lager container and reinstalls all pip_packages inside it.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("box", "config", "apply", "--yes", "--box", box)
