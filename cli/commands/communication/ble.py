# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.ble.commands

    Commands for BLE
"""
from __future__ import annotations

import re
import json

import click
from texttable import Texttable

# Import consolidated helpers from cli.core.net_helpers
from ...core.net_helpers import resolve_box, run_impl_script
from ...context import get_impl_path
from ...options import force_command_option


@click.group(name='ble')
@force_command_option
def ble():
    """Scan and connect to Bluetooth Low Energy devices"""
    pass


ADDRESS_NAME_RE = re.compile(r'\A([0-9A-F]{2}-){5}[0-9A-F]{2}\Z')
# BLE address format: XX:XX:XX:XX:XX:XX (colon-separated) or XX-XX-XX-XX-XX-XX (dash-separated)
BLE_ADDRESS_RE = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')


def check_name(device):
    return 0 if ADDRESS_NAME_RE.search(device['name']) else 1


def normalize_device(device):
    (address, data) = device
    item = {'address': address}
    manufacturer_data = data.get('manufacturer_data', {})
    for (k, v) in manufacturer_data.items():
        manufacturer_data[k] = bytes(v) if isinstance(v, list) else v
    item.update(data)
    return item


def _run_ble_command(ctx: click.Context, box_ip: str, args_dict: dict) -> None:
    """Run BLE impl script with JSON arguments."""
    try:
        run_impl_script(
            ctx,
            box_ip,
            "ble.py",
            args=(json.dumps(args_dict),),
        )
    except Exception as e:
        click.secho(f"Error executing BLE command: {e}", fg='red', err=True)
        ctx.exit(1)


def _validate_ble_address(ctx: click.Context, address: str) -> None:
    """Validate BLE address format."""
    if not BLE_ADDRESS_RE.match(address):
        click.secho(f"Error: Invalid BLE address format: {address}", fg='red', err=True)
        click.secho("Expected format: XX:XX:XX:XX:XX:XX (e.g., 00:11:22:33:44:55)", err=True)
        ctx.exit(1)


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

    scan_args = {
        'action': 'scan',
        'timeout': timeout,
        'name_contains': name_contains,
        'name_exact': name_exact,
        'verbose': verbose
    }

    _run_ble_command(ctx, box_ip, scan_args)


@ble.command('info')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.argument('address', required=True)
def info(ctx, box, address):
    """
        Get BLE device information
    """
    _validate_ble_address(ctx, address)
    box_ip = resolve_box(ctx, box)

    info_args = {
        'action': 'info',
        'address': address
    }

    _run_ble_command(ctx, box_ip, info_args)


@ble.command('connect')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.argument('address', required=True)
def connect(ctx, box, address):
    """
        Connect to a BLE device
    """
    _validate_ble_address(ctx, address)
    box_ip = resolve_box(ctx, box)

    connect_args = {
        'action': 'connect',
        'address': address
    }

    _run_ble_command(ctx, box_ip, connect_args)


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

    disconnect_args = {
        'action': 'disconnect',
        'address': address
    }

    _run_ble_command(ctx, box_ip, disconnect_args)
