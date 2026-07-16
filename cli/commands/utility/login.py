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
