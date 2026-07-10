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
    validate_net_exists,
    NET_HTTP_PORT,
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


def _invoke_remote(
    ctx: click.Context,
    net_name: str,
    target_box: str,
    command: str,
) -> None:
    """Send a USB hub command over the box's warm HTTP endpoint (:9000/usb/command).

    The handler invokes the cached hub driver inside the long-lived Flask
    process (mirrors `lager supply`/`battery`) — there is no :5000 script-upload
    fallback. Any failure surfaces a clear error and exits non-zero.
    """
    import requests

    # target_box is already the resolved IP (see usb() below); no re-resolve.
    url = f"http://{target_box}:{NET_HTTP_PORT}/usb/command"
    try:
        resp = requests.post(
            url,
            json={"netname": net_name, "action": command},
            timeout=10,
        )
    except (requests.ConnectionError, requests.Timeout):
        click.secho(
            f"Error: cannot reach box at {target_box}:{NET_HTTP_PORT}. "
            f"Check network/Tailscale and that the box is online and updated.",
            fg='red', err=True,
        )
        ctx.exit(1)
    except requests.RequestException as e:
        click.secho(f"Error: USB request to box failed: {e}", fg='red', err=True)
        ctx.exit(1)

    try:
        result = resp.json()
    except ValueError:
        click.secho(
            f"Error: box returned a non-JSON response (HTTP {resp.status_code}).",
            fg='red', err=True,
        )
        ctx.exit(1)

    if resp.status_code == 200 and result.get('success'):
        message = result.get('message') or f"USB port '{net_name}' {command}d"
        click.echo(f"[OK] {message}")
        return

    error = result.get('error') or f'USB command failed (HTTP {resp.status_code})'
    if resp.status_code == 404:
        error = (f"{error}. This box image does not expose /usb/command; "
                 f"update the box.")
    click.secho(f"Error: {error}", fg='red', err=True)
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


@usb.command()
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def state(ctx, box):
    """Show whether the USB port is enabled or disabled (read-only)"""
    _run_usb_action(ctx, box, "state")


usb.net_examples = [
    "lager usb usb1 enable --box <BOX>",
    "lager usb usb1 toggle --box <BOX>",
    "lager usb usb1 state --box <BOX>   (read-only: enabled/disabled)",
    "lager usb --box <BOX>          (list USB nets)",
]
