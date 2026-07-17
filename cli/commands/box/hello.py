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


@click.command(cls=BoxCommand)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
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
        _check_gateway(version_response, resolved_box)

        box_version = None
        if version_response.status_code == 200:
            data = version_response.json()
            box_version = data.get('version') or data.get('box_version')
            if box_version == 'unknown':
                box_version = None

        if box_version:
            click.echo(f'Version: {box_version}')
        else:
            click.echo(f'Version: {click.style("Unknown", fg="yellow")}')

        # Test connectivity with /hello endpoint
        hello_url = f'http://{resolved_box}:{port}/hello'
        hello_response = requests.get(hello_url, timeout=10, **auth)
        _check_gateway(hello_response, resolved_box)

        click.echo()
        if hello_response.status_code == 200:
            click.secho(f'{display_name} is online and responding!', fg='green')
        else:
            click.secho(f'{display_name} responded with HTTP {hello_response.status_code}', fg='yellow')

    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        from ...errors import connection_error
        raise connection_error(e, host=display_name)

    click.echo()
