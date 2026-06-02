# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    USB hub commands

    Migrated to cli/commands/communication/ and refactored to use
    consolidated helpers from cli.core.net_helpers.

    Usage:
      lager usb                          -> lists USB nets
      lager usb [NET_NAME] enable         -> enable USB port
      lager usb [NET_NAME] disable        -> disable USB port
      lager usb [NET_NAME] toggle         -> toggle USB port
"""
from __future__ import annotations

import click

# Import consolidated helpers from cli.core.net_helpers
from ...core.net_group import NetGroup
from ...core.net_helpers import (
    require_netname,
    resolve_box,
    list_nets_by_role,
    display_nets_table,
    run_impl_script,
    validate_net_exists,
)
from ...context import get_default_net


USB_ROLE = "usb"


def _validate_usb_net(ctx: click.Context, box: str, net_name: str) -> dict | None:
    """
    Validate that the USB net exists before executing command.

    Returns the net record if found, or None if not found (after displaying error).
    """
    return validate_net_exists(ctx, box, net_name, USB_ROLE)


def _display_usb_nets(ctx: click.Context, box: str) -> None:
    """Display USB nets in a table."""
    nets = list_nets_by_role(ctx, box, USB_ROLE)
    display_nets_table(nets, empty_message="No USB nets found on this box.")


def _try_fast_path(
    box_ip: str,
    net_name: str,
    command: str,
) -> tuple[bool, str | None]:
    """
    POST to the box server's /usb/command on :9000.

    The fast path skips the per-call subprocess+import cost of the slow
    `:5000/python` script-upload route by invoking the cached hub driver
    inside the long-lived Flask process (mirrors `lager supply`/`battery`).

    Returns:
        (handled, message):
          handled=True, message=... -> command succeeded, print message
          handled=False, message=msg -> handler returned success=False with
            a real hardware error; surface and exit (don't retry slow path)
          handled=False, message=None -> route not present or unreachable;
            caller should fall through to the slow path
    """
    import requests

    url = f"http://{box_ip}:9000/usb/command"
    try:
        resp = requests.post(
            url,
            json={"netname": net_name, "action": command},
            timeout=10,
        )
    except (requests.ConnectionError, requests.Timeout):
        return False, None
    except Exception:
        return False, None

    if resp.status_code == 200:
        try:
            result = resp.json()
        except ValueError:
            return False, None
        if result.get('success'):
            return True, result.get('message', f"USB port '{net_name}' {command}d")
        # 200 + success=False: the handler reached the hardware and the
        # operation failed for a real reason (missing net, port-state, etc).
        # Falling back to the slow path would just reproduce the same failure
        # after another ~500ms of subprocess churn.
        return False, result.get('error') or 'USB command failed'

    if resp.status_code == 404:
        # Route missing on an older box server. Try slow path.
        return False, None

    # Other status codes (400/409/500/502) also carry a useful error body.
    try:
        result = resp.json()
        err = result.get('error')
        if err:
            return False, err
    except ValueError:
        pass
    return False, None


def _invoke_remote(
    ctx: click.Context,
    net_name: str,
    target_box: str,
    command: str,
) -> None:
    """
    Send a USB hub command to the box.

    Prefers the fast path (POST :9000/usb/command, handler invokes the
    cached driver in-process). Falls back to the slow path (`impl/usb.py`
    over :5000/python) only if the route is missing or the box is
    unreachable on :9000 — never if the handler reports a real hardware
    error, since the slow path would just reproduce it.
    """
    # target_box is already the resolved IP (see usb() below); no re-resolve.
    handled, message = _try_fast_path(target_box, net_name, command)
    if handled:
        click.echo(f"[OK] {message}")
        return
    if message is not None:
        click.secho(f"Error: {message}", fg='red', err=True)
        ctx.exit(1)

    # Fall back to the slow path: upload impl/usb.py and run via :5000/python.
    try:
        run_impl_script(
            ctx,
            target_box,
            "usb.py",
            args=(command, net_name),
        )
    except SystemExit as e:
        # Re-raise non-zero exits to preserve exit code
        if e.code != 0:
            raise
    except Exception as e:
        error_str = str(e)
        click.secho(f"Error: Failed to execute USB command", fg='red', err=True)
        if "Connection refused" in error_str:
            click.secho(f"Could not connect to box at {target_box}", err=True)
            click.secho("Check that the box is online and Docker container is running.", err=True)
        elif "timed out" in error_str.lower():
            click.secho(f"Command timed out. The USB hub may be unresponsive.", err=True)
        else:
            click.secho(f"Details: {e}", err=True)
        ctx.exit(1)


@click.group(name="usb", cls=NetGroup, invoke_without_command=True)
@click.argument("NETNAME", required=False, metavar="[NET_NAME]")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def usb(ctx, netname, box):
    """Control programmable USB hub ports"""
    # Use provided netname, or fall back to the configured default
    if netname is None:
        netname = get_default_net(ctx, 'usb')

    if netname is not None:
        ctx.obj.netname = netname

    # No subcommand → list available USB nets
    if ctx.invoked_subcommand is None:
        resolved_box = resolve_box(ctx, box)
        _display_usb_nets(ctx, resolved_box)


def _run_usb_action(ctx, box, action: str) -> None:
    """Shared body for the enable/disable/toggle subcommands."""
    netname = require_netname(ctx, "usb")
    resolved_box = resolve_box(ctx, box)

    # Validate net exists before invoking remote command
    if _validate_usb_net(ctx, resolved_box, netname) is None:
        return  # Error already displayed

    _invoke_remote(ctx, netname, resolved_box, action)


@usb.command()
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def enable(ctx, box):
    """Enable USB port (power on)"""
    _run_usb_action(ctx, box, "enable")


@usb.command()
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def disable(ctx, box):
    """Disable USB port (power off)"""
    _run_usb_action(ctx, box, "disable")


@usb.command()
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def toggle(ctx, box):
    """Toggle USB port power on/off"""
    _run_usb_action(ctx, box, "toggle")


usb.net_examples = [
    "lager usb usb1 enable --box <BOX>",
    "lager usb usb1 toggle --box <BOX>",
    "lager usb --box <BOX>          (list USB nets)",
]
