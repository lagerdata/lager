# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager mikrotik commands

Commands for managing MikroTik routers as Lager nets.
"""
from __future__ import annotations

import json

import click

from ...core.net_helpers import resolve_box, run_impl_script


def _run_mikrotik(ctx: click.Context, box_ip: str, args_dict: dict) -> None:
    """Run the MikroTik impl script with JSON arguments."""
    try:
        run_impl_script(ctx, box_ip, "mikrotik.py", args=(json.dumps(args_dict),))
    except SystemExit as e:
        if e.code != 0:
            raise
    except Exception as e:
        error_str = str(e)
        click.secho("Error: MikroTik command failed", fg="red", err=True)
        if "Connection refused" in error_str:
            click.secho(f"Could not connect to box at {box_ip}", err=True)
        elif "timed out" in error_str.lower():
            click.secho("Command timed out.", err=True)
        else:
            click.secho(f"Details: {e}", err=True)
        ctx.exit(1)


@click.group(name="mikrotik")
def mikrotik():
    """Manage MikroTik routers as Lager nets."""
    pass


@mikrotik.command("add-net")
@click.argument("name")
@click.option("--address", required=True, help="IP address of the MikroTik router")
@click.option("--username", default="admin", show_default=True, help="RouterOS username")
@click.option("--password", default="", help="RouterOS password")
@click.option("--use-ssl", is_flag=True, default=False, help="Use HTTPS instead of HTTP")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def add_net(ctx, name, address, username, password, use_ssl, box):
    """
    Register a MikroTik router as a net on the box.

    Example:

        lager mikrotik add-net router1 --address 192.168.88.1 --username admin --password secret --box mybox
    """
    box_ip = resolve_box(ctx, box)

    net_data = {
        "name": name,
        "role": "router",
        "instrument": "MikroTik_hAP",
        "address": address,
        "pin": 0,
        "location": {
            "hostname": address,
            "username": username,
            "password": password,
            "use_ssl": use_ssl,
        },
    }

    _run_mikrotik(ctx, box_ip, {"action": "add_net", "net_data": net_data})
    click.secho(f"Net '{name}' (router) added on box {box_ip}.", fg="green")


@mikrotik.command("connect")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def connect(ctx, netname, box):
    """
    Verify connectivity to a MikroTik router net.

    Example:

        lager mikrotik connect router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_mikrotik(ctx, box_ip, {"action": "connect", "netname": netname})


@mikrotik.command("interfaces")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def interfaces(ctx, netname, box):
    """
    List network interfaces on a MikroTik router net.

    Example:

        lager mikrotik interfaces router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_mikrotik(ctx, box_ip, {"action": "interfaces", "netname": netname})


@mikrotik.command("wireless-clients")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def wireless_clients(ctx, netname, box):
    """
    List currently connected wireless clients on a MikroTik router net.

    Example:

        lager mikrotik wireless-clients router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_mikrotik(ctx, box_ip, {"action": "wireless_clients", "netname": netname})


@mikrotik.command("wireless-interfaces")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def wireless_interfaces(ctx, netname, box):
    """
    List wireless interfaces and their configuration.

    Example:

        lager mikrotik wireless-interfaces router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_mikrotik(ctx, box_ip, {"action": "wireless_interfaces", "netname": netname})


@mikrotik.command("dhcp-leases")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def dhcp_leases(ctx, netname, box):
    """
    List DHCP leases (devices that have received IP addresses).

    Example:

        lager mikrotik dhcp-leases router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_mikrotik(ctx, box_ip, {"action": "dhcp_leases", "netname": netname})


@mikrotik.command("system-info")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def system_info(ctx, netname, box):
    """
    Get system resource information from a MikroTik router net.

    Example:

        lager mikrotik system-info router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_mikrotik(ctx, box_ip, {"action": "system_info", "netname": netname})


@mikrotik.command("reboot")
@click.argument("netname")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def reboot(ctx, netname, yes, box):
    """
    Reboot a MikroTik router net.

    Example:

        lager mikrotik reboot router1 --box mybox
    """
    if not yes and not click.confirm(f"Reboot router '{netname}'?", default=False):
        click.secho("Aborted.", fg="yellow")
        return

    box_ip = resolve_box(ctx, box)
    _run_mikrotik(ctx, box_ip, {"action": "reboot", "netname": netname})


@mikrotik.command("run")
@click.argument("netname")
@click.argument("path")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def run_cmd(ctx, netname, path, box):
    """
    Run an arbitrary RouterOS REST API GET call.

    PATH is the API path relative to /rest, e.g. /ip/address

    Example:

        lager mikrotik run router1 /ip/address --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_mikrotik(ctx, box_ip, {"action": "run", "netname": netname, "path": path})
