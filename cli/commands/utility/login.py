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


@click.command(name='login')
@click.argument('auth_server_url')
@click.option('--email', help='Account email')
@click.option('--password', help='Account password')
@click.option('--web', is_flag=True,
              help='Sign in in a web browser instead of typing a password here. '
                   'Works for single sign-on accounts.')
@click.option('--no-browser', is_flag=True,
              help='With --web: do not open a browser here; print a link to open on '
                   'any machine, then paste the code it shows. Implies --web.')
def login(auth_server_url, email, password, web, no_browser):
    """Log in to the auth server at AUTH_SERVER_URL.

    Stores a session token in ~/.lager_gateway_auth; subsequent lager
    commands against boxes gated by that auth server authenticate
    automatically.
    """
    web = web or no_browser
    if web:
        if email or password:
            raise click.UsageError('--web signs in through the browser; '
                                   'do not also pass --email or --password.')
        user = gateway_auth.login_web(
            auth_server_url,
            open_browser=not no_browser,
            show_link=_show_link,
            paste_prompt=lambda: click.prompt('Code shown on the page'),
        )
    else:
        if email is None:
            email = click.prompt('Email')
        if password is None:
            password = click.prompt('Password', hide_input=True)

        def mfa_prompt():
            return click.prompt('MFA code')

        user = gateway_auth.login(auth_server_url, email, password, mfa_code_prompt=mfa_prompt)
    display = user.get('displayName') or user.get('email') or email
    click.secho(f'Logged in to {auth_server_url.rstrip("/")} as {display}.', fg='green')


def _show_link(link, opened):
    if opened:
        click.echo('Opened your browser to sign in. If it did not open, go to:')
    else:
        click.echo('Open this link in a browser to sign in:')
    click.secho(f'  {link}', fg='cyan')
    if opened:
        click.echo('Waiting for you to approve the sign-in in the browser...')
    else:
        click.echo('Select Authorize on that page. It then shows a code: paste it below.')


@click.command(name='logout')
@click.argument('auth_server_url', required=False)
def logout(auth_server_url):
    """Forget the stored session for AUTH_SERVER_URL (or all of them)."""
    gateway_auth.clear_login(auth_server_url.rstrip('/') if auth_server_url else None)
    target = auth_server_url or 'all auth servers'
    click.echo(f'Logged out of {target}.')


def _session_line(url, pinned):
    """What the user can act on, not the access token's expiry.

    The access token lapses every few minutes by design and renews silently,
    so a lapsed one is only news if the renewal fails. A stored refresh
    cookie does not prove the renewal works: the server can end the session
    at any time. So an expired token is checked by renewing it, through the
    same single-flight refresh every command uses.
    """
    if pinned:
        return (f'not used: {gateway_auth.PINNED_TOKEN_ENV} is set, '
                'and commands send that token instead')
    state = gateway_auth.check_session(url)
    if state == gateway_auth.SESSION_ACTIVE:
        return 'active'
    if state == gateway_auth.SESSION_EXPIRED:
        return f'expired. Run: lager login {url}'
    return ('could not check: the auth server neither renewed it nor refused it. '
            f'Try again later. If a box asks you to sign in, run: lager login {url}')


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

    pinned = gateway_auth.pinned_token()
    for s in status:
        who = s['email'] or 'unknown user'
        click.secho(f'{s["url"]}', fg='cyan')
        click.echo(f'  Signed in as: {who}')
        click.echo(f'  Session: {_session_line(s["url"], pinned)}')
