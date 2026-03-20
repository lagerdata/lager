# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.wifi.commands

    Commands for controlling WiFi
"""
from __future__ import annotations

import json

import click
from texttable import Texttable

# Import consolidated helpers from cli.core.net_helpers
from ...core.net_helpers import resolve_box, run_impl_script
from ...options import force_command_option

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


def _run_wifi_command(ctx: click.Context, box_ip: str, args_dict: dict) -> None:
    """Run WiFi impl script with JSON arguments."""
    try:
        run_impl_script(
            ctx,
            box_ip,
            "wifi.py",
            args=(json.dumps(args_dict),),
        )
    except SystemExit as e:
        # Re-raise non-zero exits to preserve exit code
        if e.code != 0:
            raise
    except Exception as e:
        error_str = str(e)
        click.secho(f"Error: WiFi command failed", fg='red', err=True)
        if "Connection refused" in error_str:
            click.secho(f"Could not connect to box at {box_ip}", err=True)
            click.secho("Check that the box is online and Docker container is running.", err=True)
        elif "timed out" in error_str.lower():
            click.secho("WiFi command timed out.", err=True)
            click.secho("Network operations may take longer than expected.", err=True)
        else:
            click.secho(f"Details: {e}", err=True)
        ctx.exit(1)


@click.group(name='wifi', hidden=True)
@force_command_option
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

    status_args = {
        'action': 'status'
    }

    _run_wifi_command(ctx, box_ip, status_args)


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

    scan_args = {
        'action': 'scan',
        'interface': interface
    }

    _run_wifi_command(ctx, box_ip, scan_args)


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

    connect_args = {
        'action': 'connect',
        'ssid': ssid,
        'password': password,
        'interface': interface
    }

    _run_wifi_command(ctx, box_ip, connect_args)


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

    delete_args = {
        'action': 'delete',
        'ssid': ssid,
        'connection_name': ssid
    }

    _run_wifi_command(ctx, box_ip, delete_args)
