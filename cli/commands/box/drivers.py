# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.box.drivers

    Manage custom drivers on a box: instrument drivers that live on the box
    only, never in lager-cli. Once installed, a net whose role is the driver
    type is driven with `lager <type> <NET> <action>` or
    `lager custom <NET> <action>`.
"""
from __future__ import annotations

import json
import os

import click
from texttable import Texttable

from ...box_storage import resolve_and_validate_box
from ...context import get_default_box
from ...core.custom_drivers import (
    describe_actions,
    driver_request,
    forget_box_drivers,
    remember_drivers,
)
from ...core.group_usage import LagerGroup
from ...core.utils import SizeLimitExceeded, zip_dir

MANIFEST_FILE = 'lager_driver.json'
DRIVER_FILE = 'driver.py'
MAX_BUNDLE_BYTES = 5 * 1024 * 1024


def _resolve(ctx, box, read_only=False):
    if box is None:
        box = get_default_box(ctx)
    return resolve_and_validate_box(ctx, box, read_only=read_only)


def _list_entries(ctx, box_ip):
    drivers = driver_request(ctx, box_ip, 'GET', '/drivers').get('drivers') or []
    remember_drivers(box_ip, drivers)
    return drivers


@click.group(cls=LagerGroup, invoke_without_command=True)
@click.pass_context
def drivers(ctx):
    """
        Manage custom instrument drivers on a box

        A custom driver is a bundle (lager_driver.json + driver.py) that is
        stored on the box only. Install it once; then every user can drive
        its nets with `lager <type> <NET> <action>`.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@drivers.command('list')
@click.option('--box', required=False, help='Lager Box name or IP')
@click.option('--json', 'as_json', is_flag=True, help='Print the box response as JSON')
@click.pass_context
def list_cmd(ctx, box, as_json):
    """
        List the custom drivers installed on a box
    """
    box_ip = _resolve(ctx, box, read_only=True)
    entries = _list_entries(ctx, box_ip)
    if as_json:
        click.echo(json.dumps(entries, indent=2, sort_keys=True))
        return
    if not entries:
        click.echo("No custom drivers are installed on this box.")
        return
    table = Texttable()
    table.set_deco(Texttable.HEADER)
    table.set_cols_dtype(['t'] * 5)
    table.header(['Type', 'Version', 'Status', 'SHA-256', 'Nets'])
    for e in entries:
        status = e.get('status', '')
        if status == 'error':
            status = f"error: {e.get('error', '')}"
        table.add_row([e.get('type', ''), e.get('version') or '-', status,
                       (e.get('sha256') or '')[:12], ', '.join(e.get('nets') or []) or '-'])
    click.echo(table.draw())


@drivers.command('show')
@click.argument('driver_type')
@click.option('--box', required=False, help='Lager Box name or IP')
@click.pass_context
def show(ctx, driver_type, box):
    """
        Show a custom driver's actions and settings
    """
    box_ip = _resolve(ctx, box, read_only=True)
    entry = driver_request(ctx, box_ip, 'GET', f'/drivers/{driver_type}')
    if entry.get('status') != 'ok':
        click.secho(f"Driver '{driver_type}' failed to load: {entry.get('error')}",
                    fg='red', err=True)
        ctx.exit(1)
    manifest = entry['manifest']
    click.echo(f"{manifest['type']} {manifest['version']}"
               f"  (sha256 {(entry.get('sha256') or '')[:12]})")
    if manifest.get('description'):
        click.echo(manifest['description'])
    if manifest.get('params'):
        click.echo("\nNet settings (lager nets add-custom ... --param NAME=VALUE):")
        for name, spec in sorted(manifest['params'].items()):
            kind = f"{spec['role']} net" if spec['type'] == 'net' else spec['type']
            req = ', required' if spec.get('required') and 'default' not in spec else ''
            dflt = f", default {spec['default']}" if 'default' in spec else ''
            click.echo(f"  {name:<16} <{kind}{req}{dflt}>  {spec.get('help', '')}".rstrip())
    click.echo("\nActions:")
    for line in describe_actions(manifest):
        click.echo(line)
    nets = entry.get('nets') or []
    click.echo(f"\nNets: {', '.join(nets) if nets else 'none'}")


def _read_local_manifest(path):
    try:
        with open(os.path.join(path, MANIFEST_FILE), 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except FileNotFoundError:
        raise click.UsageError(f"{path} has no {MANIFEST_FILE}")
    except (OSError, ValueError) as e:
        raise click.UsageError(f"{MANIFEST_FILE} in {path} is not valid JSON: {e}")
    if not isinstance(manifest, dict) or not isinstance(manifest.get('type'), str):
        raise click.UsageError(f"{MANIFEST_FILE} in {path} has no \"type\"")
    if not os.path.isfile(os.path.join(path, DRIVER_FILE)):
        raise click.UsageError(f"{path} has no {DRIVER_FILE}")
    return manifest


@drivers.command('install')
@click.argument('bundle_dir', type=click.Path(exists=True, file_okay=False))
@click.option('--box', required=False, help='Lager Box name or IP')
@click.option('--yes', is_flag=True, help='Replace an installed driver without asking')
@click.pass_context
def install(ctx, bundle_dir, box, yes):
    """
        Install or replace a custom driver from a bundle directory

        The directory must hold lager_driver.json and driver.py. The box checks
        the manifest; nothing is installed if it is invalid.
    """
    manifest = _read_local_manifest(bundle_dir)
    dtype = manifest['type']
    box_ip = _resolve(ctx, box)

    installed = {e.get('type'): e for e in _list_entries(ctx, box_ip)}
    current = installed.get(dtype)
    if current and not yes:
        click.echo(f"Driver '{dtype}' {current.get('version') or ''} is installed on this box "
                   f"(nets: {', '.join(current.get('nets') or []) or 'none'}).")
        if not click.confirm(f"Replace it with {manifest.get('version', '')}?", default=True):
            click.echo("Cancelled.")
            return

    try:
        data = bytes(zip_dir(bundle_dir, [], max_content_size=MAX_BUNDLE_BYTES))
    except SizeLimitExceeded:
        click.secho(f"Error: {bundle_dir} holds more than "
                    f"{MAX_BUNDLE_BYTES // (1024 * 1024)} MB; a driver bundle must be smaller.",
                    fg='red', err=True)
        ctx.exit(1)
    result = driver_request(ctx, box_ip, 'PUT', f'/drivers/{dtype}', timeout=120,
                            files={'bundle': (f'{dtype}.zip', data, 'application/zip')})
    forget_box_drivers(box_ip)
    verb = 'Replaced' if result.get('replaced') else 'Installed'
    click.secho(f"{verb} driver '{dtype}' {result.get('version', '')} "
                f"(sha256 {(result.get('sha256') or '')[:12]}).", fg='green')
    if not result.get('replaced'):
        click.echo("Next, add a net that uses it:")
        click.echo(f"  lager nets add-custom <NET_NAME> {dtype} --param NAME=VALUE --box <BOX>")
        click.echo(f"See its settings with: lager drivers show {dtype} --box <BOX>")


@drivers.command('remove')
@click.argument('driver_type')
@click.option('--box', required=False, help='Lager Box name or IP')
@click.option('--force', is_flag=True, help='Remove it even if nets still use it')
@click.option('--yes', is_flag=True, help='Skip the confirmation prompt')
@click.pass_context
def remove(ctx, driver_type, box, force, yes):
    """
        Remove a custom driver from a box
    """
    box_ip = _resolve(ctx, box)
    if not yes and not click.confirm(f"Remove driver '{driver_type}' from this box?",
                                     default=False):
        click.echo("Cancelled.")
        return
    result = driver_request(ctx, box_ip, 'DELETE', f'/drivers/{driver_type}',
                            params={'force': '1'} if force else None)
    forget_box_drivers(box_ip)
    click.secho(f"Removed driver '{driver_type}'.", fg='green')
    orphaned = result.get('orphaned_nets') or []
    if orphaned:
        click.secho(f"These nets now have no driver: {', '.join(orphaned)}. "
                    f"Delete them with `lager nets delete`.", fg='yellow')
