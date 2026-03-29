# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for firmware build, flash, and DUT control."""

from __future__ import annotations

import json

from ..server import mcp, run_lager


@mcp.tool()
def flash_firmware(
    binary_path: str,
    box: str = "",
    debug_net: str = "",
    reset_after: bool = True,
) -> str:
    """Flash a firmware binary to the DUT via the debug probe.

    Args:
        binary_path: Path to the firmware binary (.elf, .hex, .bin) on the
            host or box filesystem.
        box: Box name (leave empty to use the server's configured box).
        debug_net: Debug net name (leave empty to auto-select the first debug net).
        reset_after: Reset the DUT after flashing (default: true).
    """
    from ..server_state import get_bench
    from ..config import resolve_box_name

    box_name = box or resolve_box_name()
    if not box_name:
        return json.dumps({"error": "No box configured."})

    if not debug_net:
        bench = get_bench()
        for net in bench.nets:
            if net.net_type == "debug":
                debug_net = net.name
                break
    if not debug_net:
        return json.dumps({"error": "No debug net found on this bench."})

    args = ["debug", debug_net, "flash", binary_path, "--box", box_name]
    output = run_lager(*args, timeout=120)

    result = {"status": "success" if "Error" not in output else "error", "output": output}
    if reset_after and "Error" not in output:
        reset_output = run_lager("debug", debug_net, "reset", "--box", box_name)
        result["reset_output"] = reset_output

    return json.dumps(result, indent=2)


@mcp.tool()
def reset_dut(box: str = "", debug_net: str = "") -> str:
    """Reset the DUT via the debug probe.

    Args:
        box: Box name (leave empty for configured box).
        debug_net: Debug net name (leave empty to auto-select).
    """
    from ..server_state import get_bench
    from ..config import resolve_box_name

    box_name = box or resolve_box_name()
    if not box_name:
        return json.dumps({"error": "No box configured."})

    if not debug_net:
        bench = get_bench()
        for net in bench.nets:
            if net.net_type == "debug":
                debug_net = net.name
                break
    if not debug_net:
        return json.dumps({"error": "No debug net found on this bench."})

    output = run_lager("debug", debug_net, "reset", "--box", box_name)
    return json.dumps({"status": "success" if "Error" not in output else "error", "output": output})


@mcp.tool()
def get_boot_status(box: str = "", debug_net: str = "") -> str:
    """Read the DUT's debug status (connected, halted, running).

    Args:
        box: Box name (leave empty for configured box).
        debug_net: Debug net name (leave empty to auto-select).
    """
    from ..server_state import get_bench
    from ..config import resolve_box_name

    box_name = box or resolve_box_name()
    if not box_name:
        return json.dumps({"error": "No box configured."})

    if not debug_net:
        bench = get_bench()
        for net in bench.nets:
            if net.net_type == "debug":
                debug_net = net.name
                break
    if not debug_net:
        return json.dumps({"error": "No debug net found on this bench."})

    output = run_lager("debug", debug_net, "status", "--box", box_name)
    return json.dumps({"output": output})
