# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.box.hello

    Test box connectivity and show version
"""
import click
import requests
from ...box_storage import resolve_and_validate_box_with_name
from ...core.net_group import BoxCommand
from ...core.utils import looks_like_release_tag


def _ref_suffix(box_ref):
    """Render `/status`'s `ref` next to the version, flagged when it is not a
    release tag.

    Version alone cannot answer "what is this box running": a branch not yet
    bumped past the last release declares the same `__version__` as the
    release tag, so a box on `main` and a box on the tag print identically.
    That is issue #266, and the failure mode it produced is someone running a
    test against a box they believe is on the release and getting a green
    result for unreleased code.

    Returns '' for a box that reports no ref -- one that predates
    /etc/lager/ref -- so an older box reads exactly as it did before rather
    than gaining a scary-looking blank.
    """
    if not box_ref:
        return ''
    ref_name = str(box_ref).split('@', 1)[0]
    if looks_like_release_tag(ref_name):
        return f' ({box_ref})'
    return ' ' + click.style(f'({box_ref} -- not a release build)', fg='yellow')


@click.command(cls=BoxCommand)
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
def hello(ctx, box):
    """Test box connectivity and show version"""
    # Resolve and validate the box
    resolved_box, box_name = resolve_and_validate_box_with_name(ctx, box)
    display_name = box_name or resolved_box

    # Port for the box HTTP API
    port = 9000

    # Display header
    click.echo()
    click.echo(f'Box: {display_name}')
    click.echo(f'IP: {resolved_box}')

    try:
        # Bearer header for gateway-fronted boxes ({} for plain boxes); a
        # denial raises the actionable `lager login` error via the checker.
        from ...box_storage import _check_gateway, _gateway_kwargs
        auth = _gateway_kwargs(resolved_box)

        # /status on :9000 reports the box version (read from /etc/lager/version)
        version_url = f'http://{resolved_box}:{port}/status'
        version_response = requests.get(version_url, timeout=10, **auth)
        version_response = _check_gateway(version_response, resolved_box)

        box_version = None
        box_ref = None
        if version_response.status_code == 200:
            data = version_response.json()
            box_version = data.get('version') or data.get('box_version')
            if box_version == 'unknown':
                box_version = None
            # Absent on a box that predates /etc/lager/ref, so this stays
            # None rather than displaying a placeholder that would read as
            # "no ref recorded" on a box that simply cannot report one.
            box_ref = data.get('ref')

        if box_version:
            click.echo(f'Version: {box_version}{_ref_suffix(box_ref)}')
        else:
            click.echo(f'Version: {click.style("Unknown", fg="yellow")}')

        # Test connectivity with /hello endpoint
        hello_url = f'http://{resolved_box}:{port}/hello'
        hello_response = requests.get(hello_url, timeout=10, **auth)
        hello_response = _check_gateway(hello_response, resolved_box)

        click.echo()
        if hello_response.status_code == 200:
            click.secho(f'{display_name} is online and responding!', fg='green')
        else:
            click.secho(f'{display_name} responded with HTTP {hello_response.status_code}', fg='yellow')

    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        from ...errors import connection_error
        raise connection_error(e, host=display_name)

    click.echo()
