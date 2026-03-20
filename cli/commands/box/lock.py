# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.box.lock

    Lock and unlock commands for shared box access control
"""
import getpass

import click
import requests

from ...box_storage import resolve_and_validate_box_with_name


@click.command()
@click.option('--box', required=True, help='Name of the box to lock')
@click.pass_context
def lock(ctx, box):
    """Lock a box to prevent others from using it."""
    ip, box_name = resolve_and_validate_box_with_name(ctx, box, _skip_lock_check=True)
    display_name = box_name or box

    user = getpass.getuser()

    try:
        resp = requests.post(f'http://{ip}:5000/lock', json={'user': user}, timeout=5)
    except requests.exceptions.RequestException as e:
        click.secho(f"Error: Could not reach box '{display_name}': {e}", fg='red', err=True)
        ctx.exit(1)
        return

    if resp.status_code == 200:
        data = resp.json()
        click.secho(f"Box '{display_name}' is locked by {data.get('user')}", fg='green')
    elif resp.status_code == 409:
        data = resp.json()
        lock_info = data.get('lock', {})
        click.secho(
            f"Box '{display_name}' is already locked by {lock_info.get('user')} "
            f"(since {lock_info.get('locked_at', 'unknown')})",
            fg='red', err=True,
        )
        ctx.exit(1)
    else:
        click.secho(f"Error: Unexpected response (HTTP {resp.status_code})", fg='red', err=True)
        ctx.exit(1)


@click.command()
@click.option('--box', required=True, help='Name of the box to unlock')
@click.option('--force', is_flag=True, help='Force unlock even if locked by another user')
@click.pass_context
def unlock(ctx, box, force):
    """Unlock a box so others can use it."""
    ip, box_name = resolve_and_validate_box_with_name(ctx, box, _skip_lock_check=True)
    display_name = box_name or box

    user = getpass.getuser()

    try:
        resp = requests.post(
            f'http://{ip}:5000/unlock',
            json={'user': user, 'force': force},
            timeout=5,
        )
    except requests.exceptions.RequestException as e:
        click.secho(f"Error: Could not reach box '{display_name}': {e}", fg='red', err=True)
        ctx.exit(1)
        return

    if resp.status_code == 200:
        click.secho(f"Box '{display_name}' is now unlocked", fg='green')
    elif resp.status_code == 403:
        data = resp.json()
        lock_info = data.get('lock', {})
        locked_by = lock_info.get('user', 'unknown')
        click.secho(
            f"Box '{display_name}' is locked by {locked_by}. "
            f"To force unlock: lager boxes unlock --box {display_name} --force",
            fg='red', err=True,
        )
        ctx.exit(1)
    else:
        click.secho(f"Error: Unexpected response (HTTP {resp.status_code})", fg='red', err=True)
        ctx.exit(1)
