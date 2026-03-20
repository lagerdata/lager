# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.box.hello

    Test box connectivity and show version
"""
import click
import requests
from ...box_storage import resolve_and_validate_box_with_name
from ...options import force_command_option


@click.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@force_command_option
def hello(ctx, box):
    """Test box connectivity and show version"""
    # Resolve and validate the box
    resolved_box, box_name = resolve_and_validate_box_with_name(ctx, box)

    # Port for the Python service
    port = 5000

    # Display header
    click.echo()
    click.echo(f'Box: {box_name}')
    click.echo(f'IP: {resolved_box}')

    try:
        # Query the /cli-version endpoint for version info first
        version_url = f'http://{resolved_box}:{port}/cli-version'
        version_response = requests.get(version_url, timeout=10)

        if version_response.status_code == 200:
            data = version_response.json()
            box_version = data.get('box_version')

            if box_version:
                click.echo(f'Version: {box_version}')
            else:
                click.echo(f'Version: {click.style("Unknown", fg="yellow")}')
        elif version_response.status_code == 404:
            click.echo(f'Version: {click.style("Unknown", fg="yellow")}')
        else:
            click.echo(f'Version: {click.style("Unknown", fg="yellow")}')

        # Test connectivity with /hello endpoint
        hello_url = f'http://{resolved_box}:{port}/hello'
        hello_response = requests.get(hello_url, timeout=10)

        click.echo()
        if hello_response.status_code == 200:
            click.secho(f'{box_name} is online and responding!', fg='green')
        else:
            click.secho(f'{box_name} responded with HTTP {hello_response.status_code}', fg='yellow')

    except requests.exceptions.Timeout:
        click.secho('Error: Connection timed out', fg='red', err=True)
        click.echo(err=True)
        click.echo('Possible causes:', err=True)
        click.echo('  - Box is responding slowly or overloaded', err=True)
        click.echo('  - Network latency is high', err=True)
        click.echo('  - Services are starting up', err=True)
        click.echo(err=True)
        click.echo('Try again in a few seconds or check if the box is under heavy load.', err=True)
        ctx.exit(1)

    except requests.exceptions.ConnectionError as e:
        error_str = str(e).lower()
        click.echo(err=True)

        # Distinguish connection refused from other connection errors
        if 'connection refused' in error_str or 'errno 111' in error_str or 'errno 61' in error_str:
            click.secho('Error: Connection refused', fg='red', err=True)
            click.echo(err=True)
            click.echo(f'The box at {resolved_box} is reachable but the Lager service is not running.', err=True)
            click.echo(err=True)
            click.echo('Possible causes:', err=True)
            click.echo('  - Docker container is not running', err=True)
            click.echo('  - Service crashed or failed to start', err=True)
            click.echo('  - Port 5000 is blocked by firewall', err=True)
            click.echo(err=True)
            click.echo('To check container status, SSH to the box and run:', err=True)
            click.echo(f'  ssh lagerdata@{resolved_box} "docker ps"', err=True)
        elif 'name or service not known' in error_str or 'nodename nor servname provided' in error_str:
            click.secho('Error: Could not resolve hostname', fg='red', err=True)
            click.echo(err=True)
            click.echo(f'The hostname "{resolved_box}" could not be resolved.', err=True)
            click.echo(err=True)
            click.echo('Possible causes:', err=True)
            click.echo('  - Invalid box name or IP address', err=True)
            click.echo('  - DNS lookup failed', err=True)
            click.echo('  - Tailscale not connected (if using Tailscale hostname)', err=True)
            click.echo(err=True)
            click.echo('Check your box configuration with: lager boxes', err=True)
        elif 'no route to host' in error_str:
            click.secho('Error: No route to host', fg='red', err=True)
            click.echo(err=True)
            click.echo(f'Cannot reach {resolved_box} - network path does not exist.', err=True)
            click.echo(err=True)
            click.echo('Possible causes:', err=True)
            click.echo('  - Box is on a different network', err=True)
            click.echo('  - VPN is not connected', err=True)
            click.echo('  - Firewall is blocking the connection', err=True)
        else:
            click.secho('Error: Connection failed', fg='red', err=True)
            click.echo(err=True)
            click.echo(f'Could not connect to {resolved_box}', err=True)
            click.echo(err=True)
            click.echo('Possible causes:', err=True)
            click.echo('  - Box is offline or powered down', err=True)
            click.echo('  - Network connectivity issue', err=True)
            click.echo('  - Incorrect IP address', err=True)
            click.echo(err=True)
            click.echo('Verify the box is online and check network connectivity.', err=True)
        ctx.exit(1)

    except Exception as e:
        click.secho(f'Error: {str(e)}', fg='red', err=True)
        click.echo(err=True)
        click.echo('An unexpected error occurred while connecting to the box.', err=True)
        ctx.exit(1)

    click.echo()
