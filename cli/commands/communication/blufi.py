# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.blufi.commands

    Commands for BluFi - ESP32 WiFi provisioning over BLE
"""
from __future__ import annotations

import json

import click

from ...core.group_usage import LagerGroup
from ...core.net_helpers import resolve_box, post_box_command


@click.group(name='blufi', cls=LagerGroup)
def blufi():
    """Provision ESP32 WiFi credentials over BLE (BluFi protocol)"""
    pass


def _post_blufi(ctx: click.Context, box_ip: str, action: str,
                http_timeout: float, **params) -> dict:
    """POST one action to :9000/blufi/command and return the response."""
    return post_box_command(
        ctx, box_ip, "/blufi/command", action,
        quiet=True, http_timeout=http_timeout, **params,
    )


def _print_json(value: dict) -> None:
    click.echo("\nJSON Output:")
    click.echo(json.dumps(value, indent=2))


@blufi.command('scan')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='Total time box will spend scanning for BluFi devices', default=10.0, type=click.FLOAT, show_default=True)
@click.option('--name-contains', required=False, help='Filter devices to those whose name contains this string')
def scan(ctx, box, timeout, name_contains):
    """Scan for BluFi-capable BLE devices"""
    box_ip = resolve_box(ctx, box)

    click.secho(f"Scanning for BluFi devices for {timeout} seconds...", fg='green')
    result = _post_blufi(
        ctx, box_ip, 'scan',
        http_timeout=timeout + 30.0,
        timeout=timeout,
        name_contains=name_contains,
    )
    devices = (result.get('value') or {}).get('devices', [])

    click.secho(f"Found {len(devices)} BluFi device(s)", fg='green')
    if not devices:
        click.secho("No BluFi devices found!", fg='red')
        return

    click.secho(f"\n{'Name':<30} {'Address':<17} {'RSSI'}", fg='green')
    click.secho('-' * 55, fg='green')
    for d in devices:
        name = d.get('name') or d.get('address', '')
        click.secho(f"{name:<30} {d.get('address', ''):<17} {d.get('rssi', -100)}", fg='green')

    device_data = [
        {'name': d.get('name'), 'address': d.get('address'), 'rssi': d.get('rssi', -100)}
        for d in devices
    ]
    _print_json(device_data)


@blufi.command('connect')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='BLE connection timeout in seconds', default=20.0, type=click.FLOAT, show_default=True)
@click.argument('device_name', required=True)
def connect(ctx, box, timeout, device_name):
    """Connect to a BluFi device and retrieve version and status"""
    box_ip = resolve_box(ctx, box)

    click.secho(f"Connecting to BluFi device: {device_name}", fg='green')
    result = _post_blufi(
        ctx, box_ip, 'connect',
        http_timeout=timeout + 40.0,
        device_name=device_name,
        timeout=timeout,
    )
    value = result.get('value') or {}

    click.secho(f"[OK] Connected to {device_name}", fg='green')
    click.secho("\nDevice Info:", fg='green')
    click.secho(f"  Version:    {value.get('version') or 'N/A'}", fg='green')
    click.secho(f"  Op Mode:    {value.get('opModeName')}", fg='green')
    click.secho(f"  STA Conn:   {value.get('staConnName')}", fg='green')
    click.secho(f"  SoftAP:     {value.get('softAPConn')}", fg='green')
    _print_json(value)


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

    click.secho(f"Provisioning '{ssid}' to BluFi device: {device_name}", fg='green')
    # Provisioning blocks box-side through connect + security negotiation +
    # credential push + the target joining WiFi — budget well past all of it.
    result = _post_blufi(
        ctx, box_ip, 'provision',
        http_timeout=timeout + 90.0,
        device_name=device_name,
        timeout=timeout,
        ssid=ssid,
        password=password,
    )
    value = result.get('value') or {}

    click.secho(f"\n[OK] Device connected to '{ssid}' successfully!", fg='green')
    _print_json(value)


@blufi.command('wifi-scan')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='BLE connection timeout in seconds', default=20.0, type=click.FLOAT, show_default=True)
@click.option('--scan-timeout', required=False, help='WiFi scan duration on device in seconds', default=15.0, type=click.FLOAT, show_default=True)
@click.argument('device_name', required=True)
def wifi_scan(ctx, box, timeout, scan_timeout, device_name):
    """Scan for WiFi networks via a BluFi device"""
    box_ip = resolve_box(ctx, box)

    click.secho(f"Requesting WiFi scan via {device_name} (timeout={scan_timeout}s)...", fg='green')
    result = _post_blufi(
        ctx, box_ip, 'wifi_scan',
        http_timeout=timeout + scan_timeout + 60.0,
        device_name=device_name,
        timeout=timeout,
        scan_timeout=scan_timeout,
    )
    value = result.get('value') or {}
    networks = value.get('networks', [])

    click.secho(f"Found {len(networks)} network(s)", fg='green')
    if not networks:
        click.secho("No WiFi networks found!", fg='red')
        _print_json(value)
        return

    click.secho(f"\n{'SSID':<32} {'RSSI'}", fg='green')
    click.secho('-' * 40, fg='green')
    for net in networks:
        click.secho(f"{net.get('ssid', '???'):<32} {net.get('rssi', -100)}", fg='green')

    _print_json(value)


@blufi.command('status')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='BLE connection timeout in seconds', default=20.0, type=click.FLOAT, show_default=True)
@click.argument('device_name', required=True)
def status(ctx, box, timeout, device_name):
    """Get WiFi connection status from a BluFi device"""
    box_ip = resolve_box(ctx, box)

    result = _post_blufi(
        ctx, box_ip, 'status',
        http_timeout=timeout + 40.0,
        device_name=device_name,
        timeout=timeout,
    )
    value = result.get('value') or {}

    click.secho("\nWiFi Status:", fg='green')
    click.secho(f"  Op Mode:    {value.get('opModeName')}", fg='green')
    click.secho(f"  STA Conn:   {value.get('staConnName')}", fg='green')
    click.secho(f"  SoftAP:     {value.get('softAPConn')}", fg='green')
    _print_json(value)


@blufi.command('version')
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--timeout', required=False, help='BLE connection timeout in seconds', default=20.0, type=click.FLOAT, show_default=True)
@click.argument('device_name', required=True)
def version(ctx, box, timeout, device_name):
    """Get firmware version from a BluFi device"""
    box_ip = resolve_box(ctx, box)

    result = _post_blufi(
        ctx, box_ip, 'version',
        http_timeout=timeout + 40.0,
        device_name=device_name,
        timeout=timeout,
    )
    value = result.get('value') or {}

    click.secho(f"\nFirmware Version: {value.get('version') or 'N/A'}", fg='green')
    _print_json(value)
