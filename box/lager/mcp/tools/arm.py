# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for robotic arm control."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_arm_position(box: str, net: str) -> str:
    """Read the current position of the robotic arm.

    Returns the current X, Y, Z coordinates.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Arm net name (e.g., 'arm1')
    """
    return run_lager("arm", net, "position", "--box", box)


@mcp.tool()
def lager_arm_move(
    box: str, net: str,
    x: float, y: float, z: float,
    timeout: float = 15,
) -> str:
    """Move the robotic arm to an absolute position.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Arm net name (e.g., 'arm1')
        x: Target X coordinate
        y: Target Y coordinate
        z: Target Z coordinate
        timeout: Movement timeout in seconds (default: 15)
    """
    args = [
        "arm", net, "move",
        "--x", str(x), "--y", str(y), "--z", str(z),
        "--timeout", str(timeout),
        "--yes", "--box", box,
    ]
    return run_lager(*args, timeout=int(timeout) + 10)


@mcp.tool()
def lager_arm_move_by(
    box: str, net: str,
    dx: float = 0, dy: float = 0, dz: float = 0,
    timeout: float = 15,
) -> str:
    """Move the robotic arm by a relative offset from its current position.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Arm net name (e.g., 'arm1')
        dx: X offset (default: 0)
        dy: Y offset (default: 0)
        dz: Z offset (default: 0)
        timeout: Movement timeout in seconds (default: 15)
    """
    args = ["arm", net, "move-by", "--yes", "--box", box]
    if dx != 0:
        args.extend(["--dx", str(dx)])
    if dy != 0:
        args.extend(["--dy", str(dy)])
    if dz != 0:
        args.extend(["--dz", str(dz)])
    args.extend(["--timeout", str(timeout)])
    return run_lager(*args, timeout=int(timeout) + 10)


@mcp.tool()
def lager_arm_go_home(box: str, net: str) -> str:
    """Move the robotic arm to its home position.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Arm net name (e.g., 'arm1')
    """
    return run_lager("arm", net, "go-home", "--yes", "--box", box)


@mcp.tool()
def lager_arm_enable_motor(box: str, net: str) -> str:
    """Enable the robotic arm motor.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Arm net name (e.g., 'arm1')
    """
    return run_lager("arm", net, "enable-motor", "--box", box)


@mcp.tool()
def lager_arm_disable_motor(box: str, net: str) -> str:
    """Disable the robotic arm motor.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Arm net name (e.g., 'arm1')
    """
    return run_lager("arm", net, "disable-motor", "--box", box)


@mcp.tool()
def lager_arm_set_acceleration(
    box: str, net: str,
    acceleration: float, travel: float,
    retract: float = 60,
) -> str:
    """Set robotic arm acceleration parameters.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Arm net name (e.g., 'arm1')
        acceleration: Acceleration value
        travel: Travel speed
        retract: Retract speed (default: 60)
    """
    return run_lager(
        "arm", net, "set-acceleration",
        "--acceleration", str(acceleration),
        "--travel", str(travel),
        "--retract", str(retract),
        "--box", box,
    )


@mcp.tool()
def lager_arm_read_and_save_position(box: str, net: str) -> str:
    """Save the current arm position as a calibration reference.

    Reads the current position and stores it for future reference.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Arm net name (e.g., 'arm1')
    """
    return run_lager("arm", net, "read-and-save-position", "--box", box)
