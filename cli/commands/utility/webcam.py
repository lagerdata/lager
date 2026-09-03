# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.commands.utility.webcam

Webcam streaming commands for viewing live camera feeds from box devices.

Migrated from cli/webcam/commands.py to cli/commands/utility/webcam.py.
"""

import base64
import math
import time
from datetime import datetime
from urllib.parse import quote

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
    Get the box IP address from various sources, acquiring an ephemeral lock.

    Priority:
    1. Explicit --box option (check local boxes first)
    2. Default box from context

    Returns:
        IP address string
    """
    from ...core.net_helpers import resolve_box_locked

    return resolve_box_locked(ctx, box, 'webcam')


def _resolve_box(ctx, box):
    """Resolve box name to IP address if it's a local box."""
    from ...box_storage import resolve_and_validate_box
    return resolve_and_validate_box(ctx, box)


def _gated_token(box_ip):
    """(auth_url, access_token) for an access-gated box, else (None, None).

    ``access_token`` is None when the box is gated but nothing is stored
    for its auth server — the user has not run ``lager login`` yet.
    """
    from ... import gateway_auth
    url = gateway_auth.auth_server_for_box(box_ip)
    if not url:
        return None, None
    return url, gateway_auth.access_token_for(url)


def _viewer_url(box_ip, url):
    """The stream URL as a browser can open it.

    A gated box only admits requests that carry a sign-in token, and a
    browser cannot attach one from a plain link, so the token rides in the
    query string; the gateway swaps it for a cookie on first contact.
    Ungated boxes get the URL untouched.
    """
    if not url:
        return url
    _auth_url, token = _gated_token(box_ip)
    if not token:
        return url
    sep = '&' if '?' in url else '?'
    return f"{url}{sep}token={quote(token, safe='')}"


def _gated_link_note(box_ip, box_label=None):
    """Print how long a tokenised link lasts, or how to get one."""
    auth_url, token = _gated_token(box_ip)
    if not auth_url:
        return
    box_label = box_label or box_ip
    if not token:
        click.secho(
            f"Note: this box is access-gated and no sign-in is stored for it, "
            f"so the link above will not open. Run: lager login {auth_url}",
            fg="yellow",
        )
        return
    from ... import gateway_auth
    remaining = gateway_auth._token_expires_at(token) - time.time()
    minutes = max(1, math.ceil(remaining / 60))
    click.secho(
        f"This box is access-gated: the link carries your sign-in token and "
        f"stays valid for about {minutes} minutes. "
        f'Run "lager webcam url --box {box_label}" for a fresh link.',
        fg="yellow",
    )


def _describe_origin(result):
    """'started via cli by alice' for a stream record, or '' if unknown."""
    source = result.get("source")
    if not source:
        return ""
    text = f"started via {source}"
    if result.get("started_by"):
        text += f" by {result['started_by']}"
    return text


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


def _post_webcam(ctx: click.Context, box_ip: str, net_name: str, action: str,
                 **extra) -> dict:
    """POST one webcam action to :9000/net/command and return the response."""
    return post_net_command(
        ctx, box_ip, net_name, action,
        role=WEBCAM_ROLE, quiet=True, http_timeout=WEBCAM_TIMEOUT,
        # The box builds the viewer URL from this IP (the address the user
        # reaches the box at), not the container-internal hostname.
        box_ip=box_ip,
        **extra,
    )


def _start_params():
    """Origin fields recorded on the stream so other surfaces can label it."""
    from ...box_storage import get_lager_user
    return {"source": "cli", "started_by": get_lager_user()}


def _try_post_webcam(ctx, box_ip, net_name, action, **extra):
    """Like _post_webcam but returns None on failure instead of exiting.

    Used by the *-all commands so one broken webcam doesn't abort the rest;
    post_net_command has already printed the error before raising.
    """
    try:
        return _post_webcam(ctx, box_ip, net_name, action, **extra)
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
        result = _post_webcam(ctx, box_ip, net_name, "start", **_start_params())
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
                result = _try_post_webcam(ctx, box_ip, net, "start", **_start_params())
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
                        "source": value.get("source"),
                        "started_by": value.get("started_by"),
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
@click.option("--box", required=False, help="Lager Box name or IP")
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
    "lager webcam cam1 snapshot --box <BOX> --out frame.jpg",
    "lager webcam cam1 stop --box <BOX>",
    "lager webcam url --box <BOX>           (URLs of active streams)",
    "lager webcam start-all --box <BOX>     (no NET_NAME needed)",
]


@click.command(name="start")
@click.option("--box", help="Lager Box name or IP")
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
    click.secho(f"Webcam URL: {_viewer_url(box_ip, result['url'])}", fg="cyan", bold=True)
    click.echo()
    click.echo("Open this URL in your browser to view the live feed.")
    _gated_link_note(box_ip, box)
    click.echo(f"To stop the stream: lager webcam stop {net_name} --box {box_ip}")


@click.command(name="url")
@click.option("--box", help="Lager Box name or IP")
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
        click.secho(f"  URL: {_viewer_url(box_ip, r['url'])}", fg="cyan")
        click.echo(f"  Port: {r['port']}")
        click.echo(f"  Device: {r['video_device']}")
        origin = _describe_origin(r)
        if origin:
            click.echo(f"  Origin: {origin}")
        click.echo()
    _gated_link_note(box_ip, box)


@click.command(name="snapshot")
@click.option("--box", help="Lager Box name or IP")
@click.option("--out", "out_path", type=click.Path(dir_okay=False, writable=True),
              help="Where to write the JPEG (default: <NET>-<timestamp>.jpg)")
@click.pass_context
def webcam_snapshot(ctx, box, out_path):
    """
    Save one frame from a running webcam stream as a JPEG.

    This is the right way for scripts and assistants to look at the bench:
    the frame comes back over the box's command endpoint, so nothing but
    that endpoint has to be reachable — the stream's own port is never
    dialled. The stream must already be running (``lager webcam NET start``).
    """
    net_name = getattr(ctx.obj, "netname", None)
    if not net_name:
        raise click.UsageError(
            "NET_NAME required.\n\n"
            "Usage: lager webcam [NET_NAME] snapshot --box [BOX_NAME] [--out FILE]\n"
            "Example: lager webcam webcam1 snapshot --box my-box --out frame.jpg"
        )

    box_ip = _get_box_ip_address(ctx.parent, box)
    result = _post_webcam(ctx, box_ip, net_name, "snapshot")
    value = result.get("value") or {}
    encoded = value.get("jpeg_base64")
    if not encoded:
        click.secho("Box returned no image data", fg="red", err=True)
        raise SystemExit(1)
    data = base64.b64decode(encoded)

    if not out_path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = f"{net_name}-{stamp}.jpg"
    with open(out_path, "wb") as f:
        f.write(data)
    click.echo(f"Saved {out_path} ({len(data)} bytes)")


@click.command(name="stop")
@click.option("--box", help="Lager Box name or IP")
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
@click.option("--box", help="Lager Box name or IP")
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
            click.secho(f"  URL: {_viewer_url(box_ip, r['url'])}", fg="cyan")
        else:
            click.secho(f"  {net_name}: {r.get('error', 'failed')}", fg="red")

    click.echo()
    click.echo("Open the URLs in your browser to view the live feeds.")
    _gated_link_note(box_ip, box)
    click.echo(f"To stop all streams: lager webcam stop-all --box {box_ip}")


@click.command(name="stop-all")
@click.option("--box", help="Lager Box name or IP")
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
            click.secho(f"  {net_name}: was not up", fg="yellow")
        else:
            click.secho(f"  {net_name}: {r.get('error', 'failed')}", fg="red")


# Add subcommands to the group
webcam.add_command(webcam_start)
webcam.add_command(webcam_snapshot)
webcam.add_command(webcam_url)
webcam.add_command(webcam_stop)
webcam.add_command(webcam_start_all)
webcam.add_command(webcam_stop_all)
