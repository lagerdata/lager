# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager router commands

Commands for managing routers as Lager nets.
"""
from __future__ import annotations

import json

import click

from ...core.net_helpers import resolve_box, run_impl_script


def _run_router(ctx: click.Context, box_ip: str, args_dict: dict) -> None:
    """Run the router impl script with JSON arguments."""
    try:
        run_impl_script(ctx, box_ip, "router.py", args=(json.dumps(args_dict),))
    except SystemExit as e:
        if e.code != 0:
            raise
    except Exception as e:
        error_str = str(e)
        click.secho("Error: router command failed", fg="red", err=True)
        if "Connection refused" in error_str:
            click.secho(f"Could not connect to box at {box_ip}", err=True)
        elif "timed out" in error_str.lower():
            click.secho("Command timed out.", err=True)
        else:
            click.secho(f"Details: {e}", err=True)
        ctx.exit(1)


@click.group(name="router")
def router():
    """Manage routers as Lager nets."""
    pass


@router.command("add-net")
@click.argument("name")
@click.option("--address", required=True, help="IP address of the router")
@click.option("--username", default="admin", show_default=True, help="Router username")
@click.option("--password", default="", help="Router password")
@click.option("--instrument", default="MikroTik_hAP", show_default=True, help="Router instrument type")
@click.option("--use-ssl", is_flag=True, default=False, help="Use HTTPS instead of HTTP")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def add_net(ctx, name, address, username, password, instrument, use_ssl, box):
    """
    Register a router as a net on the box.

    Example:

        lager router add-net router1 --address 192.168.88.1 --username admin --password secret --box mybox
    """
    box_ip = resolve_box(ctx, box)

    net_data = {
        "name": name,
        "role": "router",
        "instrument": instrument,
        "address": address,
        "pin": 0,
        "location": {
            "hostname": address,
            "username": username,
            "password": password,
            "use_ssl": use_ssl,
        },
    }

    _run_router(ctx, box_ip, {"action": "add_net", "net_data": net_data})
    click.secho(f"Net '{name}' (router) added on box {box_ip}.", fg="green")


@router.command("connect")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def connect(ctx, netname, box):
    """
    Verify connectivity to a router net.

    Example:

        lager router connect router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {"action": "connect", "netname": netname})


@router.command("interfaces")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def interfaces(ctx, netname, box):
    """
    List network interfaces on a router net.

    Example:

        lager router interfaces router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {"action": "interfaces", "netname": netname})


@router.command("wireless-interfaces")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def wireless_interfaces(ctx, netname, box):
    """
    List wireless interfaces and their configuration.

    Example:

        lager router wireless-interfaces router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {"action": "wireless_interfaces", "netname": netname})


@router.command("wireless-clients")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def wireless_clients(ctx, netname, box):
    """
    List currently connected wireless clients on a router net.

    Example:

        lager router wireless-clients router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {"action": "wireless_clients", "netname": netname})


@router.command("dhcp-leases")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def dhcp_leases(ctx, netname, box):
    """
    List DHCP leases (devices that have received IP addresses).

    Example:

        lager router dhcp-leases router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {"action": "dhcp_leases", "netname": netname})


@router.command("system-info")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def system_info(ctx, netname, box):
    """
    Get system resource information from a router net.

    Example:

        lager router system-info router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {"action": "system_info", "netname": netname})


@router.command("reboot")
@click.argument("netname")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def reboot(ctx, netname, yes, box):
    """
    Reboot a router net.

    Example:

        lager router reboot router1 --box mybox
    """
    if not yes and not click.confirm(f"Reboot router '{netname}'?", default=False):
        click.secho("Aborted.", fg="yellow")
        return

    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {"action": "reboot", "netname": netname})


@router.command("enable-interface")
@click.argument("netname")
@click.argument("interface")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def enable_interface(ctx, netname, interface, box):
    """
    Enable a wireless interface on a router net.

    Example:

        lager router enable-interface router1 wlan1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {"action": "enable_interface", "netname": netname,
                               "interface": interface})


@router.command("disable-interface")
@click.argument("netname")
@click.argument("interface")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def disable_interface(ctx, netname, interface, box):
    """
    Disable a wireless interface on a router net.

    Example:

        lager router disable-interface router1 wlan1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {"action": "disable_interface", "netname": netname,
                               "interface": interface})


@router.command("block-internet")
@click.argument("netname")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def block_internet(ctx, netname, box):
    """
    Block all internet access on a router net (drops forwarded traffic).

    Use 'reset' to restore access.

    Example:

        lager router block-internet router1 --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {"action": "block_internet", "netname": netname})


@router.command("reset")
@click.argument("netname")
@click.option("--ssid", default=None, help="Baseline SSID to restore on wireless interfaces")
@click.option("--password", default=None, help="Baseline WPA2 password")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def reset(ctx, netname, ssid, password, yes, box):
    """
    Reset a router net to a clean baseline state.

    Removes all test-tagged firewall rules, bandwidth limits, and access list
    entries. Re-enables DHCP and all wireless interfaces. If --ssid and
    --password are provided, a fresh baseline WPA2 network is applied.

    Example:

        lager router reset router1 --box mybox
        lager router reset router1 --ssid HomeNet --password secret123 --box mybox
    """
    if not yes and not click.confirm(f"Reset router '{netname}' to baseline?", default=False):
        click.secho("Aborted.", fg="yellow")
        return

    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {
        "action": "reset_to_defaults",
        "netname": netname,
        "baseline_ssid": ssid,
        "baseline_pass": password,
    })
    click.secho(f"Router '{netname}' reset to baseline.", fg="green")


@router.command("run")
@click.argument("netname")
@click.argument("path")
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def run_cmd(ctx, netname, path, box):
    """
    Run an arbitrary router REST API GET call.

    PATH is the API path relative to /rest, e.g. /ip/address

    Example:

        lager router run router1 /ip/address --box mybox
    """
    box_ip = resolve_box(ctx, box)
    _run_router(ctx, box_ip, {"action": "run", "netname": netname, "path": path})
