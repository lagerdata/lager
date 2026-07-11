# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.wifi.commands

    Commands for controlling WiFi
"""
from __future__ import annotations

import json

import click

# Import consolidated helpers from cli.core.net_helpers
from ...core.group_usage import LagerGroup
from ...core.net_helpers import resolve_box, post_box_command

# WiFi constraints
MAX_SSID_LENGTH = 32  # IEEE 802.11 maximum SSID length
MIN_WPA_PASSWORD_LENGTH = 8  # WPA/WPA2 minimum passphrase length
MAX_WPA_PASSWORD_LENGTH = 63  # WPA/WPA2 maximum passphrase length

# Common wireless interface names
COMMON_INTERFACES = ['wlan0', 'wlan1', 'wlp2s0', 'wlp3s0', 'wifi0']


def _validate_ssid(ctx: click.Context, ssid: str) -> None:
    """Validate SSID format and length."""
    if not ssid:
        click.secho("Error: SSID cannot be empty", fg='red', err=True)
        ctx.exit(1)

    if len(ssid) > MAX_SSID_LENGTH:
        click.secho(f"Error: SSID too long ({len(ssid)} characters)", fg='red', err=True)
        click.secho(f"Maximum SSID length is {MAX_SSID_LENGTH} characters.", err=True)
        ctx.exit(1)

    # Check for non-printable characters
    if not ssid.isprintable():
        click.secho("Error: SSID contains non-printable characters", fg='red', err=True)
        ctx.exit(1)


def _validate_password(ctx: click.Context, password: str) -> None:
    """Validate WPA/WPA2 password length (if provided)."""
    if not password:
        return  # Empty password is allowed for open networks

    if len(password) < MIN_WPA_PASSWORD_LENGTH:
        click.secho(f"Error: Password too short ({len(password)} characters)", fg='red', err=True)
        click.secho(f"WPA/WPA2 passwords must be at least {MIN_WPA_PASSWORD_LENGTH} characters.", err=True)
        ctx.exit(1)

    if len(password) > MAX_WPA_PASSWORD_LENGTH:
        click.secho(f"Error: Password too long ({len(password)} characters)", fg='red', err=True)
        click.secho(f"WPA/WPA2 passwords can be at most {MAX_WPA_PASSWORD_LENGTH} characters.", err=True)
        ctx.exit(1)


def _validate_interface(ctx: click.Context, interface: str) -> None:
    """Validate wireless interface name format."""
    if not interface:
        click.secho("Error: Interface name cannot be empty", fg='red', err=True)
        ctx.exit(1)

    # Interface names should be alphanumeric and not too long
    if len(interface) > 15:  # Linux IFNAMSIZ - 1
        click.secho(f"Error: Interface name too long: {interface}", fg='red', err=True)
        click.secho("Interface names must be 15 characters or less.", err=True)
        ctx.exit(1)

    # Warn if using non-standard interface name
    if interface not in COMMON_INTERFACES:
        click.secho(f"Note: '{interface}' is not a common wireless interface name.", fg='yellow', err=True)
        click.secho(f"Common names: {', '.join(COMMON_INTERFACES)}", err=True)


# nmcli connect can take up to ~30s on the box, plus scan latency.
_WIFI_HTTP_TIMEOUT = 60.0


def _post_wifi(ctx: click.Context, box_ip: str, action: str, **params) -> dict:
    """POST one action to :9000/wifi/command and return the response."""
    return post_box_command(
        ctx, box_ip, "/wifi/command", action,
        quiet=True, http_timeout=_WIFI_HTTP_TIMEOUT, **params,
    )


def _format_networks_table(networks: list[dict]) -> str:
    """Format scan results in the same table shape the old impl printed."""
    if not networks:
        return "No networks found!"

    lines = []
    lines.append(f"{'SSID':<25} {'Security':<10} {'Strength'}")
    lines.append("-" * 50)
    for net in networks:
        ssid = (net.get('ssid') or 'Unknown')[:24]
        security = net.get('security', 'Unknown')
        strength = net.get('strength', 0)
        lines.append(f"{ssid:<25} {security:<10} {strength}%")
    return '\n'.join(lines)


@click.group(name='wifi', cls=LagerGroup, hidden=True)
def _wifi():
    """Manage WiFi network settings"""
    pass


@_wifi.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def status(ctx, box):
    """
        Get the current WiFi Status of the box
    """
    box_ip = resolve_box(ctx, box)

    result = _post_wifi(ctx, box_ip, 'status')
    interfaces = (result.get('value') or {}).get('interfaces', [])

    click.secho("WiFi Status:", fg='green')
    click.echo("=" * 40)
    for info in interfaces:
        connected = info.get('state', '').startswith('Connected')
        click.secho(f"Interface: {info.get('interface')}", fg='green')
        click.secho(f"    SSID:  {info.get('ssid')}",
                    fg='green' if connected else 'red')
        click.secho(f"    State: {info.get('state')}",
                    fg='green' if connected else 'red')
        click.echo()


@_wifi.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--interface', required=False, help='Wireless interface to use', default='wlan0')
def access_points(ctx, box, interface='wlan0'):
    """
        Get WiFi access points visible to the box
    """
    # Validate interface name
    _validate_interface(ctx, interface)

    box_ip = resolve_box(ctx, box)

    click.secho(f"Scanning for WiFi networks on {interface}...", fg='green')
    result = _post_wifi(ctx, box_ip, 'scan', interface=interface)
    networks = (result.get('value') or {}).get('access_points', [])

    click.secho(f"\nFound {len(networks)} network(s):", fg='green')
    click.secho(_format_networks_table(networks), fg='green')

    click.secho("\nJSON Output:", fg='green')
    click.echo(json.dumps({'access_points': networks}, indent=2))


@_wifi.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--ssid', required=True, help='SSID of the network to connect to')
@click.option('--interface', help='Wireless interface to use', default='wlan0', show_default=True)
@click.option('--password', required=False, help='Password of the network to connect to', default='')
def connect(ctx, box, ssid, interface, password=''):
    """
        Connect the box to a new network
    """
    # Validate inputs
    _validate_ssid(ctx, ssid)
    _validate_password(ctx, password)
    _validate_interface(ctx, interface)

    box_ip = resolve_box(ctx, box)

    click.secho(f"Connecting to WiFi network: {ssid}", fg='green')
    result = _post_wifi(ctx, box_ip, 'connect',
                        ssid=ssid, password=password, interface=interface)

    click.secho(f"[OK] Successfully connected to {ssid}", fg='green')
    click.echo("\nJSON Output:")
    click.echo(json.dumps(result.get('value') or {}, indent=2))


@_wifi.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--yes', is_flag=True, help='Confirm the action without prompting')
@click.argument('SSID', required=True)
def delete_connection(ctx, box, yes, ssid):
    """
        Delete the specified network from the box
    """
    # Validate SSID format
    _validate_ssid(ctx, ssid)

    if not yes and not click.confirm('An ethernet connection will be required to bring the box back online. Proceed?', default=False):
        click.echo("Aborting")
        return

    box_ip = resolve_box(ctx, box)

    click.secho(f"Deleting WiFi connection: {ssid}", fg='green')
    result = _post_wifi(ctx, box_ip, 'delete', ssid=ssid, connection_name=ssid)

    click.secho(f"[OK] Successfully deleted connection: {ssid}", fg='green')
    click.echo("\nJSON Output:")
    click.echo(json.dumps(result.get('value') or {}, indent=2))
