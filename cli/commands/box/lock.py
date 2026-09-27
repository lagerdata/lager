# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.box.lock

    Lock and unlock commands for shared box access control
"""
import click
import requests

from ...box_storage import (
    format_lock_user,
    get_lager_user,
    get_lock_holder,
    holder_is_ours,
    resolve_and_validate_box_with_name,
)


@click.command()
@click.option('--box', required=True, help='Name of the box to lock')
@click.option('--user', 'lock_user', default=None, help='Username to lock as (useful when running inside Docker where the user is otherwise root)')
@click.pass_context
def lock(ctx, box, lock_user):
    """Lock a box to prevent others from using it"""
    ip, box_name = resolve_and_validate_box_with_name(ctx, box, _skip_lock_check=True)
    display_name = box_name or box

    user = lock_user if lock_user else get_lager_user()

    if user == 'root':
        click.secho('Warning: locking as root (likely running inside a Docker container).', fg='yellow', err=True)
        click.secho('To lock as yourself, use: lager boxes lock --box ' + display_name + ' --user [USERNAME]', fg='yellow', err=True)
        click.secho('Or set permanently: lager defaults add --user [USERNAME]', fg='yellow', err=True)
        click.echo()

    try:
        # `lager boxes lock` is an explicit, persistent reservation: no TTL,
        # no heartbeat. The server's `holder_type: "user"` branch keeps the
        # lock immune to the new heartbeat-driven auto-reap that ephemeral
        # `lager python` locks participate in.
        from ...gateway_auth import auth_headers_for_box
        from ...box_storage import _check_gateway
        resp = requests.post(
            f'http://{ip}:9000/lock',
            headers=auth_headers_for_box(ip),
            json={
                'user': user,
                'holder_type': 'user',
                'ttl_seconds': None,
            },
            timeout=5,
        )
        resp = _check_gateway(resp, ip)
    except requests.exceptions.RequestException as e:
        click.secho(f"Error: Box '{display_name}' did not answer: {e}", fg='red', err=True)
        ctx.exit(1)
        return

    if resp.status_code == 200:
        data = resp.json()
        click.secho(f"Box '{display_name}' is locked by {format_lock_user(data.get('user'))}", fg='green')
    elif resp.status_code == 409:
        data = resp.json()
        lock_info = data.get('lock', {})
        click.secho(
            f"Box '{display_name}' is already locked by {format_lock_user(lock_info.get('user'))} "
            f"(since {lock_info.get('locked_at', 'unknown')})",
            fg='red', err=True,
        )
        ctx.exit(1)
    else:
        click.secho(f"Error: Unexpected response (HTTP {resp.status_code})", fg='red', err=True)
        ctx.exit(1)


def _stored_holder_if_ours(ip, holder, user):
    """Return the holder string the box stored when that lock is ours, else None.

    The box releases a lock only for the exact holder string it stored. A CI
    job's auto-lock ends in the pid of the process that took it, and a lock
    written by another tool carries a longer identity, so sending the plain
    user refused a lock that every other lager command already treats as
    ours. Sending the stored string works against every box version, and if
    the lock changes between this read and the unlock, the box refuses it.

    Stricter than the pre-command check: a ``ci:generic`` scope is shared by
    every CI job on a host, and unlock must not release a sibling job's live
    lock (``coarse_scope_ok=False``).
    """
    from ...gateway_auth import auth_headers_for_box
    from ...box_storage import _check_gateway
    try:
        resp = requests.get(
            f'http://{ip}:9000/lock',
            headers=auth_headers_for_box(ip),
            timeout=5,
        )
        resp = _check_gateway(resp, ip)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return None
    if not isinstance(data, dict) or not data.get('locked'):
        return None
    stored = data.get('user')
    if stored and holder_is_ours(stored, holder, user, coarse_scope_ok=False):
        return stored
    return None


@click.command()
@click.option('--box', required=True, help='Name of the box to unlock')
@click.option('--user', 'unlock_user', default=None,
              help='Holder to unlock as, for a lock recorded under a name '
                   'other than your user name')
@click.option('--force', is_flag=True, help='Force unlock even if locked by another user')
@click.pass_context
def unlock(ctx, box, unlock_user, force):
    """Unlock a box so others can use it"""
    ip, box_name = resolve_and_validate_box_with_name(ctx, box, _skip_lock_check=True)
    display_name = box_name or box

    user = unlock_user or get_lager_user()
    if not force:
        # Release a lock this CLI already treats as ours under the exact
        # holder string the box stored; see _stored_holder_if_ours.
        holder = unlock_user or get_lock_holder()
        user = _stored_holder_if_ours(ip, holder, user) or user

    try:
        from ...gateway_auth import auth_headers_for_box
        from ...box_storage import _check_gateway
        resp = requests.post(
            f'http://{ip}:9000/unlock',
            headers=auth_headers_for_box(ip),
            json={'user': user, 'force': force},
            timeout=5,
        )
        # Gateway denials raise here with the actionable sign-in error; the
        # 403 branch below keeps meaning "locked by another user" only (a
        # plain box's application 403 has no discovery header).
        resp = _check_gateway(resp, ip)
    except requests.exceptions.RequestException as e:
        click.secho(f"Error: Box '{display_name}' did not answer: {e}", fg='red', err=True)
        ctx.exit(1)
        return

    if resp.status_code == 200:
        click.secho(f"Box '{display_name}' is now unlocked", fg='green')
    elif resp.status_code == 403:
        data = resp.json()
        lock_info = data.get('lock', {})
        locked_by = format_lock_user(lock_info.get('user', 'unknown'))
        click.secho(
            f"Box '{display_name}' is locked by {locked_by}. "
            f"To force unlock: lager boxes unlock --box {display_name} --force",
            fg='red', err=True,
        )
        ctx.exit(1)
    else:
        click.secho(f"Error: Unexpected response (HTTP {resp.status_code})", fg='red', err=True)
        ctx.exit(1)
