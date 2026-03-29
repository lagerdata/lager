# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for Lager box management (nets, hello, instruments, boxes)."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_list_nets(box: str) -> str:
    """List all configured nets (hardware connections) on a Lager box.

    Nets are named references to physical hardware connections (I2C buses,
    SPI ports, power supplies, ADC channels, GPIO pins, etc.).

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("nets", "--box", box)


@mcp.tool()
def lager_hello(box: str) -> str:
    """Quick connectivity check for a Lager box.

    Faster and lighter than full status check. Returns basic
    connectivity confirmation.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("hello", "--box", box)


@mcp.tool()
def lager_instruments(box: str) -> str:
    """List all hardware instruments attached to a Lager box.

    Shows instrument types (LabJack, Rigol, Aardvark, etc.), their
    connection strings, and associated nets.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("instruments", "--box", box)


@mcp.tool()
def lager_boxes_list() -> str:
    """List all saved Lager boxes and their IP addresses."""
    return run_lager("boxes")


@mcp.tool()
def lager_boxes_add(name: str, ip: str) -> str:
    """Add a new Lager box by name and IP address.

    Args:
        name: Box name (e.g., 'DEMO')
        ip: Tailscale IP address of the box
    """
    return run_lager("boxes", "add", "--name", name, "--ip", ip, "--yes")


@mcp.tool()
def lager_boxes_delete(name: str) -> str:
    """Delete a saved Lager box.

    Args:
        name: Box name to delete
    """
    return run_lager("boxes", "delete", "--name", name, "--yes")


@mcp.tool()
def lager_boxes_add_all() -> str:
    """Auto-discover and add all Lager boxes from the Tailscale network.

    Scans the Tailscale network for devices with names 5-8 characters
    long and adds them as boxes.
    """
    return run_lager("boxes", "add-all", "--yes")


@mcp.tool()
def lager_boxes_delete_all() -> str:
    """Delete all saved Lager box configurations.

    WARNING: This removes every saved box. This cannot be undone.
    """
    return run_lager("boxes", "delete-all", "--yes")


@mcp.tool()
def lager_boxes_edit(
    name: str,
    ip: str = None, new_name: str = None,
    user: str = None, version: str = None,
) -> str:
    """Edit an existing Lager box configuration.

    At least one of ip, new_name, user, or version must be provided.

    Args:
        name: Current box name to edit
        ip: New IP address for the box
        new_name: New name for the box
        user: New SSH username
        version: New box version/branch
    """
    args = ["boxes", "edit", "--name", name, "--yes"]
    if ip is not None:
        args.extend(["--ip", ip])
    if new_name is not None:
        args.extend(["--new-name", new_name])
    if user is not None:
        args.extend(["--user", user])
    if version is not None:
        args.extend(["--version", version])
    return run_lager(*args)


@mcp.tool()
def lager_boxes_export(output: str = "") -> str:
    """Export box configuration to JSON.

    Args:
        output: File path to write JSON to (omit to return JSON directly)
    """
    if output:
        return run_lager("boxes", "export", "-o", output)
    return run_lager("boxes", "export")


@mcp.tool()
def lager_boxes_import(file: str, merge: bool = False) -> str:
    """Import box configuration from a JSON file.

    Args:
        file: Path to the JSON configuration file
        merge: Merge with existing boxes instead of replacing (default: false)
    """
    args = ["boxes", "import", file, "--yes"]
    if merge:
        args.append("--merge")
    return run_lager(*args)


@mcp.tool()
def lager_nets_add(
    box: str, name: str, role: str, channel: str, address: str,
) -> str:
    """Create a new net (hardware connection) on a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
        name: Net name to create (e.g., 'i2c1')
        role: Net role/type (e.g., 'i2c', 'spi', 'power_supply')
        channel: Hardware channel identifier
        address: Device address or connection string
    """
    return run_lager(
        "nets", "add", name, role, channel, address, "--box", box,
    )


@mcp.tool()
def lager_nets_delete(box: str, name: str, net_type: str) -> str:
    """Delete a net (hardware connection) from a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
        name: Net name to delete
        net_type: Net type (e.g., 'i2c', 'spi', 'power_supply')
    """
    return run_lager(
        "nets", "delete", name, net_type, "--yes", "--box", box,
    )


@mcp.tool()
def lager_nets_rename(box: str, name: str, new_name: str) -> str:
    """Rename a net on a Lager box.

    Args:
        box: Box name (e.g., 'DEMO')
        name: Current net name
        new_name: New name for the net
    """
    return run_lager("nets", "rename", name, new_name, "--box", box)


@mcp.tool()
def lager_nets_add_all(box: str) -> str:
    """Auto-create all possible nets from instruments connected to a box.

    Discovers all connected instruments and creates nets for every
    available channel.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("nets", "add-all", "--yes", "--box", box)


@mcp.tool()
def lager_nets_add_batch(box: str, json_file: str) -> str:
    """Add multiple nets from a JSON file.

    The JSON file should contain an array of net definitions, each with
    name, role, channel, and address fields.

    Args:
        box: Box name (e.g., 'DEMO')
        json_file: Path to JSON file with net definitions
    """
    return run_lager("nets", "add-batch", json_file, "--box", box)


@mcp.tool()
def lager_nets_delete_all(box: str) -> str:
    """Delete all nets from a Lager box.

    WARNING: This removes every saved net on the box. This cannot be undone.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("nets", "delete-all", "--yes", "--box", box)


@mcp.tool()
def lager_nets_set_script(box: str, name: str, script_path: str) -> str:
    """Attach a J-Link script to a debug net.

    The script is stored on the box and used automatically during
    connect, flash, erase, and reset operations.

    Args:
        box: Box name (e.g., 'DEMO')
        name: Debug net name (e.g., 'debug1')
        script_path: Local path to the J-Link script file
    """
    return run_lager("nets", "set-script", name, script_path, "--box", box)


@mcp.tool()
def lager_nets_remove_script(box: str, name: str) -> str:
    """Remove a J-Link script from a debug net.

    Args:
        box: Box name (e.g., 'DEMO')
        name: Debug net name (e.g., 'debug1')
    """
    return run_lager("nets", "remove-script", name, "--box", box)


@mcp.tool()
def lager_nets_show_script(box: str, name: str) -> str:
    """Display the J-Link script attached to a debug net.

    Args:
        box: Box name (e.g., 'DEMO')
        name: Debug net name (e.g., 'debug1')
    """
    return run_lager("nets", "show-script", name, "--box", box)
