# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for robotic arm control via direct on-box Net API."""

import json

from ..server import mcp


@mcp.tool()
def arm_position(net: str) -> str:
    """Read the current X, Y, Z position of the robotic arm.

    Args:
        net: Arm net name (e.g., 'arm1')
    """
    from lager import Net, NetType

    x, y, z = Net.get(net, type=NetType.Arm).position()
    return json.dumps({"status": "ok", "net": net, "x": x, "y": y, "z": z})


@mcp.tool()
def arm_move(net: str, x: float, y: float, z: float, timeout: float = 15.0) -> str:
    """Move the robotic arm to an absolute position.

    Args:
        net: Arm net name (e.g., 'arm1')
        x: Target X coordinate (mm)
        y: Target Y coordinate (mm)
        z: Target Z coordinate (mm)
        timeout: Movement timeout in seconds (default: 15)
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Arm).move_to(x, y, z, timeout=timeout)
    return json.dumps({"status": "ok", "net": net, "x": x, "y": y, "z": z})


@mcp.tool()
def arm_move_relative(
    net: str,
    dx: float = 0.0,
    dy: float = 0.0,
    dz: float = 0.0,
    timeout: float = 15.0,
) -> str:
    """Move the robotic arm by a relative offset from its current position.

    Args:
        net: Arm net name (e.g., 'arm1')
        dx: X offset in mm (default: 0)
        dy: Y offset in mm (default: 0)
        dz: Z offset in mm (default: 0)
        timeout: Movement timeout in seconds (default: 15)
    """
    from lager import Net, NetType

    new_pos = Net.get(net, type=NetType.Arm).move_relative(dx, dy, dz, timeout=timeout)
    x, y, z = new_pos
    return json.dumps({"status": "ok", "net": net, "x": x, "y": y, "z": z})


@mcp.tool()
def arm_go_home(net: str) -> str:
    """Move the robotic arm to its home position.

    Args:
        net: Arm net name (e.g., 'arm1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Arm).go_home()
    return json.dumps({"status": "ok", "net": net, "action": "go_home"})


@mcp.tool()
def arm_enable_motor(net: str) -> str:
    """Enable the robotic arm motor.

    Args:
        net: Arm net name (e.g., 'arm1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Arm).enable_motor()
    return json.dumps({"status": "ok", "net": net, "motor": "enabled"})


@mcp.tool()
def arm_disable_motor(net: str) -> str:
    """Disable the robotic arm motor.

    Args:
        net: Arm net name (e.g., 'arm1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Arm).disable_motor()
    return json.dumps({"status": "ok", "net": net, "motor": "disabled"})


@mcp.tool()
def arm_save_position(net: str) -> str:
    """Save the current arm position as a calibration reference.

    Args:
        net: Arm net name (e.g., 'arm1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Arm).save_position()
    return json.dumps({"status": "ok", "net": net, "action": "save_position"})
