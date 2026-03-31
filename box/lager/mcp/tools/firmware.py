# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for firmware flash and DUT control.

Supports two modes:
  1. Path-based: binary already on the box filesystem
  2. Content-based: binary content sent inline (base64) from the host

Mode 2 is the typical agent workflow — the agent compiles firmware on
the host machine and sends the binary content through the MCP tool,
just like `lager debug flash` does via the CLI.
"""

import base64
import json
import tempfile
import os

from mcp.server.fastmcp import Context

from ..server import mcp


def _find_debug_net() -> str:
    """Return the first debug net on the bench, or empty string."""
    from ..server_state import get_bench

    for net in get_bench().nets:
        if net.net_type == "debug":
            return net.name
    return ""


@mcp.tool()
async def flash_firmware(
    ctx: Context,
    binary_path: str = "",
    binary_content_base64: str = "",
    file_type: str = "hex",
    debug_net: str = "",
    reset_after: bool = True,
) -> str:
    """Flash firmware to the DUT and optionally reset.

    Provide EITHER binary_path (file already on the box) OR
    binary_content_base64 (base64-encoded binary from the host).

    Args:
        binary_path: Path to firmware file on the box filesystem.
        binary_content_base64: Base64-encoded firmware binary (sent from host).
        file_type: Firmware file type — 'hex', 'elf', or 'bin' (default: hex).
        debug_net: Debug net name (leave empty to auto-select).
        reset_after: Reset the DUT after flashing (default: true).
    """
    from lager import Net, NetType

    total = 3 if reset_after else 2

    net_name = debug_net or _find_debug_net()
    if not net_name:
        return json.dumps({"status": "error", "error": "No debug net found on this bench."})

    if not binary_path and not binary_content_base64:
        return json.dumps({"status": "error", "error": "Provide binary_path or binary_content_base64."})

    path_to_flash = binary_path
    tmp_file = None

    if binary_content_base64:
        try:
            content = base64.b64decode(binary_content_base64)
        except Exception as exc:
            return json.dumps({"status": "error", "error": f"Invalid base64: {exc}"})

        suffix = {"hex": ".hex", "elf": ".elf", "bin": ".bin"}.get(file_type, ".hex")
        tmp_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp_file.write(content)
        tmp_file.close()
        path_to_flash = tmp_file.name

    await ctx.report_progress(progress=0, total=total)

    try:
        dbg = Net.get(net_name, type=NetType.Debug)
        dbg.flash(path_to_flash)
        await ctx.report_progress(progress=1, total=total)

        result = {"status": "ok", "net": net_name, "file_type": file_type}
        if binary_path:
            result["firmware_path"] = binary_path
        else:
            result["uploaded_bytes"] = len(content)

        if reset_after:
            dbg.reset()
            result["reset"] = True
            await ctx.report_progress(progress=2, total=total)

        await ctx.report_progress(progress=total, total=total)
        return json.dumps(result)
    finally:
        if tmp_file:
            os.unlink(tmp_file.name)


@mcp.tool()
def reset_dut(debug_net: str = "") -> str:
    """Reset the DUT via the debug probe.

    Args:
        debug_net: Debug net name (leave empty to auto-select).
    """
    from lager import Net, NetType

    net_name = debug_net or _find_debug_net()
    if not net_name:
        return json.dumps({"status": "error", "error": "No debug net found on this bench."})

    Net.get(net_name, type=NetType.Debug).reset()
    return json.dumps({"status": "ok", "net": net_name, "reset": True})


@mcp.tool()
def get_boot_status(debug_net: str = "") -> str:
    """Read the DUT's debug probe connection status.

    Args:
        debug_net: Debug net name (leave empty to auto-select).
    """
    from lager import Net, NetType

    net_name = debug_net or _find_debug_net()
    if not net_name:
        return json.dumps({"status": "error", "error": "No debug net found on this bench."})

    dbg = Net.get(net_name, type=NetType.Debug)
    try:
        info = dbg.status()
        return json.dumps({"status": "ok", "net": net_name, "debug_status": info}, default=str)
    except Exception as exc:
        return json.dumps({"status": "ok", "net": net_name, "debug_status": str(exc)})
