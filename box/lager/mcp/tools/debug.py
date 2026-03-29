# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for firmware debugging (flash, reset, erase, memory read)."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_debug_list_nets(box: str) -> str:
    """List available debug nets on a box.

    Shows all configured debug nets with their probe type and
    target device information.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("debug", "--box", box)


@mcp.tool()
def lager_debug_flash(
    box: str, net: str,
    hex_file: str = "", elf_file: str = "", bin_file: str = "",
    erase: bool = False,
) -> str:
    """Flash firmware to a debug target.

    Provide exactly one firmware file (hex, elf, or bin).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Debug net name (e.g., 'debug1')
        hex_file: Path to .hex firmware file
        elf_file: Path to .elf firmware file
        bin_file: Path to .bin firmware file (format: 'file.bin@0x08000000')
        erase: Erase all flash before programming (default: false)
    """
    args = ["debug", net, "flash", "--box", box]
    if hex_file:
        args.extend(["--hex", hex_file])
    if elf_file:
        args.extend(["--elf", elf_file])
    if bin_file:
        args.extend(["--bin", bin_file])
    if erase:
        args.append("--erase")
    return run_lager(*args)


@mcp.tool()
def lager_debug_reset(box: str, net: str) -> str:
    """Reset the debug target device.

    Performs a hardware reset via the debug probe.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Debug net name (e.g., 'debug1')
    """
    return run_lager("debug", net, "reset", "--box", box)


@mcp.tool()
def lager_debug_erase(box: str, net: str) -> str:
    """Erase all flash memory on the debug target.

    WARNING: This erases the entire flash. The device will have no firmware
    after this operation.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Debug net name (e.g., 'debug1')
    """
    return run_lager("debug", net, "erase", "--yes", "--box", box)


@mcp.tool()
def lager_debug_status(box: str, net: str) -> str:
    """Show debug net status and connection information.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Debug net name (e.g., 'debug1')
    """
    return run_lager("debug", net, "status", "--box", box)


@mcp.tool()
def lager_debug_memrd(box: str, net: str, start_addr: str, length: str) -> str:
    """Read memory from the debug target.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Debug net name (e.g., 'debug1')
        start_addr: Start address in hex (e.g., '0x08000000')
        length: Number of bytes to read in hex or decimal (e.g., '0x100' or '256')
    """
    return run_lager("debug", net, "memrd", start_addr, length, "--box", box)


@mcp.tool()
def lager_debug_health(box: str, net: str) -> str:
    """Run a health check on the debug probe connection.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Debug net name (e.g., 'debug1')
    """
    return run_lager("debug", net, "health", "--box", box)


@mcp.tool()
def lager_debug_disconnect(box: str, net: str) -> str:
    """Disconnect the debug probe from the target.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Debug net name (e.g., 'debug1')
    """
    return run_lager("debug", net, "disconnect", "--box", box)


@mcp.tool()
def lager_debug_gdbserver(
    box: str, net: str,
    speed: str = None, force: bool = False,
    halt: bool = False, reset: bool = False,
    gdb_port: int = 2331,
) -> str:
    """Start JLinkGDBServer for firmware debugging.

    Starts a GDB server that listens for connections from arm-none-eabi-gdb.
    The server remains running until explicitly disconnected.
    Note: Interactive RTT streaming (--rtt/--rtt-reset) is excluded.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Debug net name (e.g., 'debug1')
        speed: SWD/JTAG speed in kHz (e.g., '4000') or 'adaptive'
        force: Force new connection even if already connected (default: false)
        halt: Halt the device when connecting (default: false)
        reset: Reset the device after starting GDB server (default: false)
        gdb_port: GDB server port (default: 2331)
    """
    args = ["debug", net, "gdbserver", "--box", box,
            "--gdb-port", str(gdb_port)]
    if speed is not None:
        args.extend(["--speed", speed])
    if force:
        args.append("--force")
    if halt:
        args.append("--halt")
    if reset:
        args.append("--reset")
    return run_lager(*args)
