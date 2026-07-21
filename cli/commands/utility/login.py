# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
`lager login` / `lager logout` -- authenticate against a box access gateway.

Only needed for boxes fronted by an authenticating gateway. A plain Lager
box never asks for this; when a gated box rejects a command it prints the
exact `lager login <url>` to run.
"""
import click

from ... import gateway_auth
from ...gateway_auth import ACCESS_DOCS_URL


def _format_duration(seconds):
    """Human 'in 12m' / '3m ago' from a signed seconds value."""
    if seconds is None:
        return 'unknown'
    mins = int(abs(seconds) // 60)
    unit = f'{mins}m' if mins < 60 else f'{mins // 60}h{mins % 60:02d}m'
    return f'in {unit}' if seconds >= 0 else f'{unit} ago'


@click.command(name='login')
@click.argument('auth_server_url')
@click.option('--email', prompt=True, help='Account email')
@click.option('--password', prompt=True, hide_input=True, help='Account password')
def login(auth_server_url, email, password):
    """Log in to the auth server at AUTH_SERVER_URL.

    Stores a session token in ~/.lager_gateway_auth; subsequent lager
    commands against boxes gated by that auth server authenticate
    automatically.
    """
    def mfa_prompt():
        return click.prompt('MFA code')

    user = gateway_auth.login(auth_server_url, email, password, mfa_code_prompt=mfa_prompt)
    display = user.get('displayName') or user.get('email') or email
    click.secho(f'Logged in to {auth_server_url.rstrip("/")} as {display}.', fg='green')


@click.command(name='logout')
@click.argument('auth_server_url', required=False)
def logout(auth_server_url):
    """Forget the stored session for AUTH_SERVER_URL (or all of them)."""
    gateway_auth.clear_login(auth_server_url.rstrip('/') if auth_server_url else None)
    target = auth_server_url or 'all auth servers'
    click.echo(f'Logged out of {target}.')


@click.command(name='whoami')
def whoami():
    """Show your access-gateway sign-in status.

    Reports which auth servers you're signed in to, as whom, when each
    session expires, and which boxes are known to be gated by each — the
    first thing to check when a box reports an authorization problem.
    """
    status = gateway_auth.auth_status()
    if not status:
        click.echo('Not signed in to any access gateway.')
        click.echo('Plain Lager boxes need no sign-in. If a box asks for it, run:')
        click.secho('  lager login <auth-server-url>', fg='cyan')
        click.echo(f'Details: {ACCESS_DOCS_URL}')
        return

    for s in status:
        who = s['email'] or 'unknown user'
        click.secho(f'{s["url"]}', fg='cyan')
        click.echo(f'  Signed in as: {who}')
        exp = s['expires_in']
        if exp is None:
            state = 'unknown'
        elif exp >= 0:
            state = f'valid ({_format_duration(exp)})'
        elif s['refreshable']:
            state = f'expired {_format_duration(exp)} — auto-renews on next use'
        else:
            state = f'expired {_format_duration(exp)} — run: lager login {s["url"]}'
        click.echo(f'  Session: {state}')
        if s['boxes']:
            click.echo(f'  Gated boxes seen: {", ".join(s["boxes"])}')
