# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.blufi.commands

    Commands for BluFi - ESP32 WiFi provisioning over BLE
"""
from __future__ import annotations

import json

import click

from ...core.net_helpers import resolve_box, run_impl_script
from ...options import force_command_option


@click.group(name='blufi')
@force_command_option
def blufi():
    """Provision ESP32 WiFi credentials over BLE (BluFi protocol)"""
    pass


def _run_blufi_command(ctx: click.Context, box_ip: str, args_dict: dict) -> None:
    """Run BluFi impl script with JSON arguments."""
    try:
        run_impl_script(
            ctx,
            box_ip,
            "blufi.py",
            args=(json.dumps(args_dict),),
        )
    except Exception as e:
        click.secho(f"Error executing BluFi command: {e}", fg='red', err=True)
        ctx.exit(1)


@blufi.command('scan')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='Total time box will spend scanning for BluFi devices', default=10.0, type=click.FLOAT, show_default=True)
@click.option('--name-contains', required=False, help='Filter devices to those whose name contains this string')
def scan(ctx, box, timeout, name_contains):
    """Scan for BluFi-capable BLE devices"""
    box_ip = resolve_box(ctx, box)

    scan_args = {
        'action': 'scan',
        'timeout': timeout,
        'name_contains': name_contains,
    }

    _run_blufi_command(ctx, box_ip, scan_args)


@blufi.command('connect')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='BLE connection timeout in seconds', default=20.0, type=click.FLOAT, show_default=True)
@click.argument('device_name', required=True)
def connect(ctx, box, timeout, device_name):
    """Connect to a BluFi device and retrieve version and status"""
    box_ip = resolve_box(ctx, box)

    connect_args = {
        'action': 'connect',
        'device_name': device_name,
        'timeout': timeout,
    }

    _run_blufi_command(ctx, box_ip, connect_args)


@blufi.command('provision')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='BLE connection timeout in seconds', default=20.0, type=click.FLOAT, show_default=True)
@click.option('--ssid', required=True, help='WiFi network SSID to provision')
@click.option('--password', required=True, help='WiFi network password')
@click.argument('device_name', required=True)
def provision(ctx, box, timeout, ssid, password, device_name):
    """Provision WiFi credentials to a BluFi device"""
    box_ip = resolve_box(ctx, box)

    provision_args = {
        'action': 'provision',
        'device_name': device_name,
        'timeout': timeout,
        'ssid': ssid,
        'password': password,
    }

    _run_blufi_command(ctx, box_ip, provision_args)


@blufi.command('wifi-scan')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='BLE connection timeout in seconds', default=20.0, type=click.FLOAT, show_default=True)
@click.option('--scan-timeout', required=False, help='WiFi scan duration on device in seconds', default=15.0, type=click.FLOAT, show_default=True)
@click.argument('device_name', required=True)
def wifi_scan(ctx, box, timeout, scan_timeout, device_name):
    """Scan for WiFi networks via a BluFi device"""
    box_ip = resolve_box(ctx, box)

    wifi_scan_args = {
        'action': 'wifi_scan',
        'device_name': device_name,
        'timeout': timeout,
        'scan_timeout': scan_timeout,
    }

    _run_blufi_command(ctx, box_ip, wifi_scan_args)


@blufi.command('status')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='BLE connection timeout in seconds', default=20.0, type=click.FLOAT, show_default=True)
@click.argument('device_name', required=True)
def status(ctx, box, timeout, device_name):
    """Get WiFi connection status from a BluFi device"""
    box_ip = resolve_box(ctx, box)

    status_args = {
        'action': 'status',
        'device_name': device_name,
        'timeout': timeout,
    }

    _run_blufi_command(ctx, box_ip, status_args)


@blufi.command('version')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='BLE connection timeout in seconds', default=20.0, type=click.FLOAT, show_default=True)
@click.argument('device_name', required=True)
def version(ctx, box, timeout, device_name):
    """Get firmware version from a BluFi device"""
    box_ip = resolve_box(ctx, box)

    version_args = {
        'action': 'version',
        'device_name': device_name,
        'timeout': timeout,
    }

    _run_blufi_command(ctx, box_ip, version_args)
