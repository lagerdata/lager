# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.ble.commands

    Commands for BLE
"""
from __future__ import annotations

import re
import json

import click

from ...core.group_usage import LagerGroup
from ...core.net_helpers import resolve_box, post_box_command


@click.group(name='ble', cls=LagerGroup)
def ble():
    """Scan and connect to Bluetooth Low Energy devices"""
    pass


ADDRESS_NAME_RE = re.compile(r'\A([0-9A-F]{2}-){5}[0-9A-F]{2}\Z')
# BLE address format: XX:XX:XX:XX:XX:XX (colon-separated) or XX-XX-XX-XX-XX-XX (dash-separated)
BLE_ADDRESS_RE = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')


def check_name(device):
    return 0 if ADDRESS_NAME_RE.search(device['name']) else 1


def _post_ble(ctx: click.Context, box_ip: str, action: str,
              http_timeout: float, **params) -> dict:
    """POST one action to :9000/ble/command and return the response."""
    return post_box_command(
        ctx, box_ip, "/ble/command", action,
        quiet=True, http_timeout=http_timeout, **params,
    )


def _validate_ble_address(ctx: click.Context, address: str) -> None:
    """Validate BLE address format."""
    if not BLE_ADDRESS_RE.match(address):
        click.secho(f"Error: Invalid BLE address format: {address}", fg='red', err=True)
        click.secho("Expected format: XX:XX:XX:XX:XX:XX (e.g., 00:11:22:33:44:55)", err=True)
        ctx.exit(1)


def _format_device_table(devices: list[dict], verbose: bool = False) -> str:
    """Format scan results in the same table shape the old impl printed."""
    lines = []
    if verbose:
        lines.append(f"{'Name':<20} {'Address':<17} {'RSSI':<6} {'UUIDs'}")
        lines.append("-" * 80)
    else:
        lines.append(f"{'Name':<20} {'Address':<17} {'RSSI'}")
        lines.append("-" * 50)

    for device in devices:
        name = device.get('name') or device.get('address', '')
        address = device.get('address', '')
        rssi = device.get('rssi', -100)
        if verbose:
            uuids = device.get('uuids', [])
            uuids_str = ', '.join(str(u)[:8] + '...' for u in uuids[:3])
            if len(uuids) > 3:
                uuids_str += f" (+{len(uuids)-3} more)"
            lines.append(f"{name:<20} {address:<17} {rssi:<6} {uuids_str}")
        else:
            lines.append(f"{name:<20} {address:<17} {rssi}")

    return '\n'.join(lines)


def _print_services(services: list[dict]) -> None:
    """Print a service/characteristic summary for info/connect output."""
    for i, service in enumerate(services):
        desc = service.get('description') or 'Unknown Service'
        click.secho(f"  {i+1}. {service['uuid']}", fg='green')
        click.secho(f"     Description: {desc}", fg='green')
        chars = service.get('characteristics', [])
        click.secho(f"     Characteristics: {len(chars)}", fg='green')
        for char in chars[:3]:
            props = ', '.join(char.get('properties', []))
            click.secho(f"       - {char['uuid'][:8]}... [{props}]", fg='green')
        if len(chars) > 3:
            click.secho(f"       ... and {len(chars)-3} more", fg='green')


@ble.command('scan')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='Total time box will spend scanning for devices', default=5.0, type=click.FLOAT, show_default=True)
@click.option('--name-contains', required=False, help='Filter devices to those whose name contains this string')
@click.option('--name-exact', required=False, help='Filter devices to those whose name matches this string')
@click.option('--verbose', required=False, is_flag=True, default=False, help='Verbose output (includes UUIDs)')
def scan(ctx, box, timeout, name_contains, name_exact, verbose):
    """
        Scan for BLE devices
    """
    # Validate timeout range
    MIN_TIMEOUT, MAX_TIMEOUT = 0.1, 300.0
    if timeout < MIN_TIMEOUT or timeout > MAX_TIMEOUT:
        click.secho(f"Error: Timeout must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds, got {timeout}", fg='red', err=True)
        ctx.exit(1)

    box_ip = resolve_box(ctx, box)

    click.secho(f"Scanning for BLE devices for {timeout} seconds...", fg='green')
    result = _post_ble(
        ctx, box_ip, 'scan',
        http_timeout=timeout + 30.0,
        timeout=timeout,
        name_contains=name_contains,
        name_exact=name_exact,
    )

    devices = (result.get('value') or {}).get('devices', [])
    click.secho(f"Found {len(devices)} device(s)", fg='green')

    if not devices:
        if name_exact or name_contains:
            click.secho("No devices found matching filter criteria!", fg='red')
        else:
            click.secho("No BLE devices found!", fg='red')
        return

    click.secho("\n" + _format_device_table(devices, verbose), fg='green')

    # Structured data for programmatic use, matching the old script's output.
    device_data = []
    for device in devices:
        item = {
            'name': device.get('name'),
            'address': device.get('address'),
            'rssi': device.get('rssi', -100),
        }
        if verbose:
            item['uuids'] = device.get('uuids', [])
        device_data.append(item)

    click.echo("\nJSON Output:")
    click.echo(json.dumps(device_data, indent=2))


def _info_or_connect(ctx, box, address, connect_style: bool):
    """Shared body for the info and connect commands (same box action)."""
    _validate_ble_address(ctx, address)
    box_ip = resolve_box(ctx, box)

    verb = "Connecting to" if connect_style else "Getting info for"
    click.secho(f"{verb} BLE device: {address}", fg='green')

    result = _post_ble(ctx, box_ip, 'connect' if connect_style else 'info',
                       http_timeout=45.0, address=address)
    value = result.get('value') or {}
    services = value.get('services', [])

    if connect_style:
        click.secho(f"[OK] Connected to {address}", fg='green')
        click.secho("\nConnection successful!", fg='green')
        click.secho(f"Device: {address}", fg='green')
        click.secho(f"Services: {len(services)}", fg='green')
        if services:
            click.secho("\nServices found:", fg='green')
            for i, service in enumerate(services[:3]):
                chars = service.get('characteristics', [])
                click.secho(f"  {i+1}. {service['uuid'][:8]}... ({len(chars)} characteristics)", fg='green')
            if len(services) > 3:
                click.secho(f"  ... and {len(services)-3} more services", fg='green')
    else:
        click.secho("\nDevice Information:", fg='green')
        click.secho(f"Address: {address}", fg='green')
        click.secho(f"Services: {len(services)}", fg='green')
        if services:
            click.secho("\nServices:", fg='green')
            _print_services(services)

    click.echo("\nJSON Output:")
    click.echo(json.dumps(value, indent=2))


@ble.command('info')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.argument('address', required=True)
def info(ctx, box, address):
    """
        Get BLE device information
    """
    _info_or_connect(ctx, box, address, connect_style=False)


@ble.command('connect')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.argument('address', required=True)
def connect(ctx, box, address):
    """
        Connect to a BLE device
    """
    _info_or_connect(ctx, box, address, connect_style=True)


@ble.command('disconnect')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.argument('address', required=True)
def disconnect(ctx, box, address):
    """
        Disconnect from a BLE device
    """
    _validate_ble_address(ctx, address)
    box_ip = resolve_box(ctx, box)

    click.secho(f"Disconnecting from BLE device: {address}", fg='green')
    result = _post_ble(ctx, box_ip, 'disconnect', http_timeout=45.0, address=address)
    value = result.get('value') or {}

    click.secho(f"[OK] Disconnected from {address}", fg='green')
    if value.get('note'):
        click.secho(f"  Note: {value['note']}", fg='green')

    click.echo("\nJSON Output:")
    click.echo(json.dumps(value, indent=2))
