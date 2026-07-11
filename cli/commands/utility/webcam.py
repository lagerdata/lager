# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.commands.utility.webcam

Webcam streaming commands for viewing live camera feeds from box devices.

Migrated from cli/webcam/commands.py to cli/commands/utility/webcam.py.
"""

import click
from texttable import Texttable
from ...context import get_default_box, get_default_net
from ...core.net_group import NetGroupHelpMixin
from ...core.net_helpers import list_nets_by_role, post_net_command

# The box saves webcam nets with role "webcam" (NetType.from_role); the CLI
# historically filtered on "camera", so listing never matched anything.
WEBCAM_ROLE = "webcam"

# Timeout for webcam commands (seconds)
WEBCAM_TIMEOUT = 30


def _get_box_ip_address(ctx: click.Context, box: str = None) -> str:
    """
    Get the box IP address from various sources.

    Priority:
    1. Explicit --box option (check local boxes first)
    2. Default box from context

    Returns:
        IP address string
    """
    from ...box_storage import resolve_and_validate_box

    return resolve_and_validate_box(ctx, box)


def _resolve_box(ctx, box):
    """Resolve box name to IP address if it's a local box."""
    from ...box_storage import resolve_and_validate_box
    return resolve_and_validate_box(ctx, box)


def _list_webcam_nets(ctx, box):
    """Get list of webcam nets from box (GET :9000/nets/list)."""
    return list_nets_by_role(ctx, box, WEBCAM_ROLE)


def _display_webcam_nets(ctx, box):
    """Display webcam nets in a table."""
    nets = _list_webcam_nets(ctx, box)
    if not nets:
        click.echo("No webcam nets found on this box.")
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


def _post_webcam(ctx: click.Context, box_ip: str, net_name: str, action: str) -> dict:
    """POST one webcam action to :9000/net/command and return the response."""
    return post_net_command(
        ctx, box_ip, net_name, action,
        role=WEBCAM_ROLE, quiet=True, http_timeout=WEBCAM_TIMEOUT,
        # The box builds the viewer URL from this IP (the address the user
        # reaches the box at), not the container-internal hostname.
        box_ip=box_ip,
    )


def _try_post_webcam(ctx, box_ip, net_name, action):
    """Like _post_webcam but returns None on failure instead of exiting.

    Used by the *-all commands so one broken webcam doesn't abort the rest;
    post_net_command has already printed the error before raising.
    """
    try:
        return _post_webcam(ctx, box_ip, net_name, action)
    except SystemExit:
        return None


def _run_webcam_command(ctx: click.Context, box_ip: str, action: str, net_name: str = None) -> dict:
    """
    Execute webcam command via the box HTTP API (POST :9000/net/command).

    Single-net actions (start/stop) map directly onto the box's webcam role;
    the *-all actions iterate the box's webcam nets client-side.

    Args:
        ctx: Click context
        box_ip: Box IP address
        action: start, stop, start-all, stop-all, or url-all
        net_name: Name of the webcam net (single-net actions only)

    Returns:
        dict: Result in the same shape the old impl script produced

    Raises:
        SystemExit: On command failure (single-net actions)
    """
    if action == "start":
        result = _post_webcam(ctx, box_ip, net_name, "start")
        value = result.get("value") or {}
        return {
            "ok": True,
            "url": value.get("url"),
            "port": value.get("port"),
            "already_running": value.get("already_running", False),
        }

    if action == "stop":
        result = _post_webcam(ctx, box_ip, net_name, "stop")
        value = result.get("value") or {}
        return {"ok": value.get("stopped", False),
                "message": result.get("message")}

    if action in ("start-all", "stop-all", "url-all"):
        webcam_nets = [n.get("name") for n in _list_webcam_nets(ctx, box_ip)]
        if not webcam_nets:
            return {"ok": True, "message": "No webcam nets found", "results": []}

        results = []
        for net in webcam_nets:
            if action == "start-all":
                result = _try_post_webcam(ctx, box_ip, net, "start")
                if result is None:
                    results.append({"net": net, "success": False, "error": "failed"})
                else:
                    value = result.get("value") or {}
                    results.append({
                        "net": net,
                        "success": True,
                        "url": value.get("url"),
                        "already_running": value.get("already_running", False),
                    })
            elif action == "stop-all":
                result = _try_post_webcam(ctx, box_ip, net, "stop")
                if result is None:
                    results.append({"net": net, "success": False, "error": "failed"})
                else:
                    stopped = (result.get("value") or {}).get("stopped", False)
                    results.append({"net": net, "success": True,
                                    "was_running": stopped})
            else:  # url-all
                result = _try_post_webcam(ctx, box_ip, net, "status")
                if result is None:
                    continue
                value = result.get("value") or {}
                if value.get("running"):
                    results.append({
                        "net": net,
                        "url": value.get("url"),
                        "port": value.get("port"),
                        "video_device": value.get("video_device"),
                    })

        if action == "url-all" and not results:
            return {"ok": True, "message": "No active webcam streams", "results": []}
        return {"ok": True, "results": results}

    raise click.UsageError(f"Unknown webcam action: {action}")


class WebcamGroup(NetGroupHelpMixin, click.Group):
    """Custom Group that handles optional NETNAME before subcommand"""

    def parse_args(self, ctx, args):
        """Override parse_args to handle NETNAME before subcommand"""
        # List of commands that don't require NETNAME
        command_names = ['url', 'start-all', 'stop-all']

        # Check if first argument is a command name (without NETNAME)
        if args and args[0] in command_names:
            # No NETNAME provided, just parse normally
            return super().parse_args(ctx, args)

        # Check if we have at least 2 args and second one is a command
        if len(args) >= 2 and args[1] in list(self.commands.keys()):
            # First arg is NETNAME, second is command
            netname = args[0]
            ctx.obj.netname = netname
            # Remove NETNAME from args and continue parsing
            return super().parse_args(ctx, args[1:])

        # Check if first argument is a command but no NETNAME provided
        if args and args[0] in list(self.commands.keys()):
            # Try to get default netname
            netname = get_default_net(ctx, 'webcam')
            if netname:
                ctx.obj.netname = netname
            # Continue parsing normally
            return super().parse_args(ctx, args)

        # Default parsing
        return super().parse_args(ctx, args)


@click.group(name="webcam", cls=WebcamGroup, invoke_without_command=True)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def webcam(ctx, box):
    """Manage webcam streams"""
    # If no subcommand was provided
    if ctx.invoked_subcommand is None:
        if box:
            # List webcam nets for the specified box
            target_box = _resolve_box(ctx, box)
            _display_webcam_nets(ctx, target_box)
        else:
            # Show help if no --box and no subcommand
            click.echo(ctx.get_help())


webcam.net_examples = [
    "lager webcam cam1 start --box <BOX>",
    "lager webcam cam1 stop --box <BOX>",
    "lager webcam url --box <BOX>           (URLs of active streams)",
    "lager webcam start-all --box <BOX>     (no NET_NAME needed)",
]


@click.command(name="start")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def webcam_start(ctx, box):
    """
    Start webcam stream
    """
    # Get netname from parent context
    net_name = getattr(ctx.obj, "netname", None)
    if not net_name:
        raise click.UsageError(
            "NET_NAME required.\n\n"
            "Usage: lager webcam [NET_NAME] start --box [BOX_NAME]\n"
            "Example: lager webcam webcam1 start --box my-box"
        )

    # Use parent context for get_default_box to access the correct params
    box_ip = _get_box_ip_address(ctx.parent, box)

    click.echo(f"Starting webcam stream for net '{net_name}' on {box_ip}...")

    result = _run_webcam_command(ctx, box_ip, "start", net_name)

    if result.get("already_running"):
        click.secho(f"Stream already running for '{net_name}'", fg="yellow")
    else:
        click.secho(f"Stream started successfully", fg="green")

    click.echo()
    click.secho(f"Webcam URL: {result['url']}", fg="cyan", bold=True)
    click.echo()
    click.echo("Open this URL in your browser to view the live feed.")
    click.echo(f"To stop the stream: lager webcam stop {net_name} --box {box_ip}")


@click.command(name="url")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def webcam_url(ctx, box):
    """
    Print URLs of all active webcam streams
    """
    # Use parent context for get_default_box to access the correct params
    box_ip = _get_box_ip_address(ctx.parent, box)

    result = _run_webcam_command(ctx, box_ip, "url-all", None)

    if not result.get("results"):
        click.secho("No active webcam streams found", fg="yellow")
        return

    click.echo(f"Active webcam streams on {box_ip}:")
    click.echo()

    for r in result["results"]:
        click.secho(f"{r['net']}:", fg="green", bold=True)
        click.secho(f"  URL: {r['url']}", fg="cyan")
        click.echo(f"  Port: {r['port']}")
        click.echo(f"  Device: {r['video_device']}")
        click.echo()


@click.command(name="stop")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def webcam_stop(ctx, box):
    """
    Stop webcam stream
    """
    # Get netname from parent context
    net_name = getattr(ctx.obj, "netname", None)
    if not net_name:
        raise click.UsageError(
            "NET_NAME required.\n\n"
            "Usage: lager webcam [NET_NAME] stop --box [BOX_NAME]\n"
            "Example: lager webcam webcam1 stop --box my-box"
        )

    # Use parent context for get_default_box to access the correct params
    box_ip = _get_box_ip_address(ctx.parent, box)

    click.echo(f"Stopping webcam stream for net '{net_name}'...")

    result = _run_webcam_command(ctx, box_ip, "stop", net_name)

    if result.get("ok"):
        click.secho("Stream stopped successfully", fg="green")
    else:
        click.secho(result.get("message", "Stream not running"), fg="yellow")


@click.command(name="start-all")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def webcam_start_all(ctx, box):
    """
    Start all webcam streams
    """
    # Use parent context for get_default_box to access the correct params
    box_ip = _get_box_ip_address(ctx.parent, box)

    click.echo(f"Starting all webcam streams on {box_ip}...")

    result = _run_webcam_command(ctx, box_ip, "start-all", None)

    if not result.get("results"):
        click.secho(result.get("message", "No webcam nets found"), fg="yellow")
        return

    click.echo()
    success_count = len([r for r in result["results"] if r["success"]])
    click.secho(f"Started {success_count}/{len(result['results'])} webcam streams", fg="green")
    click.echo()

    # Print results for each webcam
    for r in result["results"]:
        net_name = r["net"]
        if r["success"]:
            status = "already running" if r.get("already_running") else "started"
            click.secho(f"  {net_name}: {status}", fg="green")
            click.secho(f"  URL: {r['url']}", fg="cyan")
        else:
            click.secho(f"  {net_name}: {r.get('error', 'failed')}", fg="red")

    click.echo()
    click.echo("Open the URLs in your browser to view the live feeds.")
    click.echo(f"To stop all streams: lager webcam stop-all --box {box_ip}")


@click.command(name="stop-all")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def webcam_stop_all(ctx, box):
    """
    Stop all webcam streams
    """
    # Use parent context for get_default_box to access the correct params
    box_ip = _get_box_ip_address(ctx.parent, box)

    click.echo(f"Stopping all webcam streams on {box_ip}...")

    result = _run_webcam_command(ctx, box_ip, "stop-all", None)

    if not result.get("results"):
        click.secho(result.get("message", "No webcam nets found"), fg="yellow")
        return

    click.echo()
    stopped_count = len([r for r in result["results"] if r.get("was_running")])
    click.secho(f"Stopped {stopped_count} webcam streams", fg="green")

    # Print results for each webcam
    for r in result["results"]:
        net_name = r["net"]
        if r.get("was_running"):
            click.secho(f"  {net_name}: stopped", fg="green")
        elif r["success"]:
            click.secho(f"  {net_name}: was not running", fg="yellow")
        else:
            click.secho(f"  {net_name}: {r.get('error', 'failed')}", fg="red")


# Add subcommands to the group
webcam.add_command(webcam_start)
webcam.add_command(webcam_url)
webcam.add_command(webcam_stop)
webcam.add_command(webcam_start_all)
webcam.add_command(webcam_stop_all)
