# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Robot arm control commands.

This module provides CLI commands for controlling robot arm position and movement.
All coordinates are in millimeters (mm).
"""
import click
from texttable import Texttable

from ...context import get_default_box, get_default_net
from ...core.net_group import NetGroup
from ...core.net_helpers import (
    resolve_box,
    list_nets_by_role,
    validate_net_exists,
    post_net_command,
)

ARM_ROLE = "arm"


def _list_arm_nets(ctx, box):
    """Get list of arm nets from box using net_helpers."""
    return list_nets_by_role(ctx, box, ARM_ROLE)


def _display_arm_nets(ctx, box):
    """Display arm nets in a table."""
    nets = _list_arm_nets(ctx, box)
    if not nets:
        click.echo("No arm nets found on this box.")
        return

    table = Texttable()
    table.set_deco(Texttable.HEADER)
    table.set_cols_dtype(["t", "t", "t", "t", "t"])
    table.set_cols_align(["l", "l", "l", "l", "l"])
    table.header(["Name", "Net Type", "Instrument", "Channel", "Address"])

    for rec in nets:
        table.add_row([
            rec.get("name", ""),
            rec.get("role", ""),
            rec.get("instrument", ""),
            rec.get("pin", ""),
            rec.get("address", "")
        ])

    click.echo(table.draw())


def _run(ctx, payload: dict, box: str):
    """Drive the arm over the box HTTP API (POST :9000/net/command)."""
    payload = dict(payload)
    netname = payload.pop("netname")
    action = payload.pop("command")

    # Moves block on the box until the arm reaches the target (or the move
    # timeout expires), so the HTTP budget must outlast the move timeout.
    http_timeout = 45.0
    if action in ("move", "move_by"):
        http_timeout = float(payload.get("timeout") or 15.0) + 30.0

    result = post_net_command(
        ctx, box, netname, action,
        role=ARM_ROLE, quiet=True, http_timeout=http_timeout, **payload,
    )
    message = result.get("message")
    if message:
        click.secho(message, fg="green")


def _require_netname(ctx) -> str:
    """Get arm netname from context or raise error."""
    net = getattr(ctx.obj, "arm_netname", None) if ctx.obj is not None else None
    if not net:
        raise click.UsageError("NETNAME is required for this command.")
    return net


def _validate_arm_net(ctx, box, netname) -> bool:
    """Validate that the arm net exists on the box. Returns False if invalid."""
    result = validate_net_exists(ctx, box, netname, ARM_ROLE)
    return result is not None


def _resolve_box_for_command(ctx, target_box):
    """Resolve box from command-level --box option or group-level stored box."""
    if target_box:
        return resolve_box(ctx, target_box)
    # Fall back to box stored by the group command
    return getattr(ctx.obj, "resolved_box", None) or get_default_box(ctx)


@click.group(
    name="arm",
    cls=NetGroup,
    invoke_without_command=True,
    context_settings={"max_content_width": 100},
    help="Control robot arm position and movement (units: mm)",
)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.argument("netname", required=False, metavar="[NET_NAME]")
def arm(ctx, box, netname):
    """
    Usage: lager arm [NET_NAME] [COMMAND] --box [BOX_NAME]
    """
    # Preserve whatever the top-level CLI stored in ctx.obj (don't replace with dict)
    if ctx.obj is None:
        # create a simple attribute container without nuking future expectations
        class _Obj: pass
        ctx.obj = _Obj()

    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'arm')

    # Store arm-specific fields as attributes so get_default_box(ctx) still works
    setattr(ctx.obj, "arm_netname", netname)

    # Only resolve box if box is provided at group level
    # Otherwise, let subcommands resolve it
    if box:
        resolved = resolve_box(ctx, box)
        setattr(ctx.obj, "resolved_box", resolved)
    else:
        # Don't set box - let subcommands handle it
        setattr(ctx.obj, "resolved_box", None)

    # If no subcommand, list nets
    if ctx.invoked_subcommand is None:
        resolved = resolve_box(ctx, box)
        _display_arm_nets(ctx, resolved)


arm.net_examples = [
    "lager arm arm1 position --box <BOX>",
    "lager arm arm1 move --x 100 --y 200 --z 50 --box <BOX>",
    "lager arm arm1 go-home --box <BOX>",
    "lager arm --box <BOX>                  (list arm nets)",
]


@arm.command(name="position", help="Print current arm position (mm)")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def position(ctx, box):
    net = _require_netname(ctx)
    resolved = _resolve_box_for_command(ctx, box)
    if not _validate_arm_net(ctx, resolved, net):
        return
    _run(ctx, {"netname": net, "command": "position"}, resolved)


@arm.command(name="move", help="Move arm to absolute XYZ position (mm)")
@click.option("--timeout", type=click.FloatRange(min=0.1), default=15.0, show_default=True, help="Move timeout (s)")
@click.option("--x", "x", type=float, required=True, help="X coordinate (mm)")
@click.option("--y", "y", type=float, required=True, help="Y coordinate (mm)")
@click.option("--z", "z", type=float, required=True, help="Z coordinate (mm)")
@click.option("--yes", is_flag=True, help="Confirm the action without prompting")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def move(ctx, timeout, x, y, z, yes, box):
    # Validate coordinate bounds (Rotrics Dexarm workspace)
    # X: left/right from center, Y: forward from base, Z: up/down from table
    ARM_MIN_X, ARM_MAX_X = -300, 300
    ARM_MIN_Y, ARM_MAX_Y = 170, 360
    ARM_MIN_Z, ARM_MAX_Z = -140, 100

    out_of_bounds = []
    if not ARM_MIN_X <= x <= ARM_MAX_X:
        out_of_bounds.append(f"  - X={x} is outside range [{ARM_MIN_X}, {ARM_MAX_X}]")
    if not ARM_MIN_Y <= y <= ARM_MAX_Y:
        out_of_bounds.append(f"  - Y={y} is outside range [{ARM_MIN_Y}, {ARM_MAX_Y}]")
    if not ARM_MIN_Z <= z <= ARM_MAX_Z:
        out_of_bounds.append(f"  - Z={z} is outside range [{ARM_MIN_Z}, {ARM_MAX_Z}]")

    if out_of_bounds:
        click.secho("Coordinates may be out of bounds:", fg='red', err=True)
        for msg in out_of_bounds:
            click.secho(msg, fg='red', err=True)
        click.secho(
            f"Approximate Dexarm bounds: X: [{ARM_MIN_X}, {ARM_MAX_X}], "
            f"Y: [{ARM_MIN_Y}, {ARM_MAX_Y}], Z: [{ARM_MIN_Z}, {ARM_MAX_Z}]",
            fg='yellow', err=True
        )
        ctx.exit(1)

    net = _require_netname(ctx)
    resolved = _resolve_box_for_command(ctx, box)
    if not _validate_arm_net(ctx, resolved, net):
        return
    if not yes and not click.confirm(f"Move arm to X={x}, Y={y}, Z={z}?", default=False):
        click.echo("Aborting")
        return
    _run(ctx, {"netname": net, "command": "move", "x": x, "y": y, "z": z, "timeout": timeout}, resolved)


@arm.command(name="move-by", help="Move arm by dX dY dZ (mm)")
@click.option("--timeout", type=click.FloatRange(min=0.1), default=15.0, show_default=True, help="Move timeout (s)")
@click.option("--dx", "dx", type=float, default=0.0, help="Delta X (mm)")
@click.option("--dy", "dy", type=float, default=0.0, help="Delta Y (mm)")
@click.option("--dz", "dz", type=float, default=0.0, help="Delta Z (mm)")
@click.option("--yes", is_flag=True, help="Confirm the action without prompting")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def delta(ctx, timeout, dx, dy, dz, yes, box):
    net = _require_netname(ctx)
    resolved = _resolve_box_for_command(ctx, box)
    if not _validate_arm_net(ctx, resolved, net):
        return
    if not yes and not click.confirm(f"Move arm by dX={dx}, dY={dy}, dZ={dz}?", default=False):
        click.echo("Aborting")
        return
    _run(ctx, {"netname": net, "command": "move_by", "dx": dx, "dy": dy, "dz": dz, "timeout": timeout}, resolved)


@arm.command(name="go-home", help="Move arm to the home position (X0 Y300 Z0)")
@click.option("--yes", is_flag=True, help="Confirm the action without prompting")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def go_home(ctx, yes, box):
    net = _require_netname(ctx)
    resolved = _resolve_box_for_command(ctx, box)
    if not _validate_arm_net(ctx, resolved, net):
        return
    if not yes and not click.confirm("Move arm to home position (X0 Y300 Z0)?", default=False):
        click.echo("Aborting")
        return
    _run(ctx, {"netname": net, "command": "go_home"}, resolved)


@arm.command(name="enable-motor", help="Enable arm motors")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def enable_motor(ctx, box):
    net = _require_netname(ctx)
    resolved = _resolve_box_for_command(ctx, box)
    if not _validate_arm_net(ctx, resolved, net):
        return
    _run(ctx, {"netname": net, "command": "enable_motor"}, resolved)


@arm.command(name="disable-motor", help="Disable arm motors")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def disable_motor(ctx, box):
    net = _require_netname(ctx)
    resolved = _resolve_box_for_command(ctx, box)
    if not _validate_arm_net(ctx, resolved, net):
        return
    _run(ctx, {"netname": net, "command": "disable_motor"}, resolved)


@arm.command(name="read-and-save-position", help="Save current position as calibration reference")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def read_and_save_position(ctx, box):
    net = _require_netname(ctx)
    resolved = _resolve_box_for_command(ctx, box)
    if not _validate_arm_net(ctx, resolved, net):
        return
    _run(ctx, {"netname": net, "command": "read_and_save_position"}, resolved)


@arm.command(name="set-acceleration", help="Set arm acceleration: acceleration, travel, retract")
@click.option("--acceleration", "acceleration", type=click.IntRange(min=1), required=True, help="Acceleration value")
@click.option("--travel", "travel", type=click.IntRange(min=1), required=True, help="Travel acceleration")
@click.option("--retract", "retract", type=click.IntRange(min=1), default=60, show_default=True, help="Retract acceleration")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def set_acceleration(ctx, acceleration, travel, retract, box):
    net = _require_netname(ctx)
    resolved = _resolve_box_for_command(ctx, box)
    if not _validate_arm_net(ctx, resolved, net):
        return
    _run(
        ctx,
        {
            "netname": net,
            "command": "set_acceleration",
            "acceleration": int(acceleration),
            "travel_acceleration": int(travel),
            "retract_acceleration": int(retract),
        },
        resolved,
    )
