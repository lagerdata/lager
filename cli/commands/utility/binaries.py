# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.binaries

    Commands for managing custom binaries on boxes
"""
import click
import requests
from pathlib import Path
from ...box_storage import resolve_and_validate_box
from ...context import get_default_box

# Container path where customer binaries are mounted
CONTAINER_BINARIES_PATH = '/home/www-data/customer-binaries'


def _format_size(size_bytes):
    """Format file size in human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != 'B' else f"{size_bytes} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


@click.group(invoke_without_command=True)
@click.pass_context
def binaries(ctx):
    """
        Manage custom binaries on boxes

        Custom binaries are executables that can be called from lager Python
        scripts via subprocess. They are stored on the box and mounted
        into the lager container at /home/www-data/customer-binaries/
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@binaries.command('add')
@click.argument('binary_path', type=click.Path(exists=True))
@click.option('--box', required=False, help='Lagerbox name or IP')
@click.option('--name', required=False, help='Name for the binary on the box (defaults to filename)')
@click.option('--yes', is_flag=True, help='Skip confirmation prompt')
@click.pass_context
def add(ctx, binary_path, box, name, yes):
    """
        Upload a binary to a box
    """
    # Resolve box
    if not box:
        box = get_default_box(ctx)

    resolved_ip = resolve_and_validate_box(ctx, box)

    # Determine binary name
    binary_path = Path(binary_path)
    binary_name = name or binary_path.name

    # Validate binary name (no path separators, spaces, etc.)
    if '/' in binary_name or '\\' in binary_name:
        click.secho("Error: Binary name cannot contain path separators", fg='red', err=True)
        ctx.exit(1)

    # Read binary content
    try:
        with open(binary_path, 'rb') as f:
            binary_content = f.read()
    except FileNotFoundError:
        click.secho(f"Error: Binary file not found: {binary_path}", fg='red', err=True)
        ctx.exit(1)
    except PermissionError:
        click.secho(f"Error: Permission denied reading binary file: {binary_path}", fg='red', err=True)
        click.secho("Check that you have read permissions for this file", err=True)
        ctx.exit(1)
    except IsADirectoryError:
        click.secho(f"Error: Path is a directory, not a file: {binary_path}", fg='red', err=True)
        ctx.exit(1)
    except Exception as e:
        click.secho(f"Error reading binary file: {e}", fg='red', err=True)
        ctx.exit(1)

    file_size = len(binary_content)
    size_str = _format_size(file_size)

    # Confirmation
    if not yes:
        click.echo(f"\nYou are about to upload:")
        click.echo(f"  Binary: {binary_path} ({size_str})")
        click.echo(f"  Name:   {binary_name}")
        click.echo(f"  To:     {box} ({resolved_ip})")
        click.echo()

        if not click.confirm("Proceed?", default=True):
            click.echo("Cancelled.")
            return

    click.echo(f"Uploading {binary_name} ({size_str})...")

    # Upload via HTTP
    url = f'http://{resolved_ip}:5000/binaries/add'
    try:
        # Send as multipart form data
        files = {
            'binary': (binary_name, binary_content, 'application/octet-stream')
        }
        data = {
            'name': binary_name
        }
        response = requests.post(url, files=files, data=data, timeout=120)

        if response.status_code == 200:
            result = response.json()
            click.echo()
            click.secho(f"Binary '{binary_name}' uploaded successfully!", fg='green')
            click.echo()
            click.echo("Use in your lager Python scripts:")
            click.echo(f"  from lager.binaries import run_custom_binary")
            click.echo(f"  result = run_custom_binary('{binary_name}', '--version')")
            click.echo()
            click.echo(f"Or call directly: {CONTAINER_BINARIES_PATH}/{binary_name}")

            if result.get('restart_required'):
                click.echo()
                click.secho("Note: Container restart may be required for first-time setup.", fg='yellow')
                click.echo("Run: lager update --box " + box)
        else:
            error_msg = response.json().get('error', response.text)
            click.secho(f"Error uploading binary: {error_msg}", fg='red', err=True)
            ctx.exit(1)

    except requests.exceptions.ConnectionError:
        click.secho(f"Error: Could not connect to box at {resolved_ip}", fg='red', err=True)
        click.secho("Possible causes:", err=True)
        click.secho("  - Box is offline or unreachable", err=True)
        click.secho("  - Lager service not running (port 5000)", err=True)
        click.secho("  - Firewall blocking connection", err=True)
        click.secho(f"Try: lager hello --box {box}", err=True)
        ctx.exit(1)
    except requests.exceptions.Timeout:
        click.secho("Error: Upload timed out", fg='red', err=True)
        click.secho("The file may be too large or the connection is slow", err=True)
        ctx.exit(1)
    except Exception as e:
        click.secho(f"Error: {e}", fg='red', err=True)
        ctx.exit(1)


@binaries.command('list')
@click.option('--box', required=False, help='Lagerbox name or IP')
@click.pass_context
def list_binaries(ctx, box):
    """
        List custom binaries on a box
    """
    # Resolve box
    if not box:
        box = get_default_box(ctx)

    resolved_ip = resolve_and_validate_box(ctx, box)

    url = f'http://{resolved_ip}:5000/binaries/list'
    try:
        response = requests.get(url, timeout=30)

        if response.status_code == 200:
            result = response.json()
            binaries_list = result.get('binaries', [])

            click.echo(f"\nCustom binaries on {box} ({resolved_ip}):")
            click.echo(f"  Host directory: {result.get('host_path', 'N/A')}")
            click.echo(f"  Container path: {CONTAINER_BINARIES_PATH}")
            click.echo()

            if not binaries_list:
                click.echo("  (no binaries installed)")
                click.echo()
                click.echo("To add a binary:")
                click.echo(f"  lager binaries add ./my_binary --box {box}")
                return

            for binary in binaries_list:
                name = binary.get('name', 'unknown')
                size = binary.get('size', 0)
                executable = binary.get('executable', False)

                size_str = _format_size(size)
                status = click.style("(executable)", fg='green') if executable else click.style("(not executable)", fg='yellow')

                click.echo(f"  - {name} ({size_str}) {status}")

            click.echo()

            if not result.get('mounted', True):
                click.secho("Note: Directory is not yet mounted in container.", fg='yellow')
                click.echo("Run: lager update --box " + box)

        else:
            error_msg = response.json().get('error', response.text)
            click.secho(f"Error listing binaries: {error_msg}", fg='red', err=True)
            ctx.exit(1)

    except requests.exceptions.ConnectionError:
        click.secho(f"Error: Could not connect to box at {resolved_ip}", fg='red', err=True)
        click.secho("Possible causes:", err=True)
        click.secho("  - Box is offline or unreachable", err=True)
        click.secho("  - Lager service not running (port 5000)", err=True)
        click.secho("  - Firewall blocking connection", err=True)
        click.secho(f"Try: lager hello --box {box}", err=True)
        ctx.exit(1)
    except requests.exceptions.Timeout:
        click.secho("Error: Request timed out", fg='red', err=True)
        ctx.exit(1)
    except Exception as e:
        click.secho(f"Error: {e}", fg='red', err=True)
        ctx.exit(1)


@binaries.command('remove')
@click.argument('binary_name')
@click.option('--box', required=False, help='Lagerbox name or IP')
@click.option('--yes', is_flag=True, help='Skip confirmation prompt')
@click.pass_context
def remove(ctx, binary_name, box, yes):
    """
        Remove a binary from a box
    """
    # Resolve box
    if not box:
        box = get_default_box(ctx)

    resolved_ip = resolve_and_validate_box(ctx, box)

    # First check if binary exists
    url = f'http://{resolved_ip}:5000/binaries/list'
    try:
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            result = response.json()
            binaries_list = [b.get('name') for b in result.get('binaries', [])]
            if binary_name not in binaries_list:
                click.secho(f"Error: Binary '{binary_name}' not found on {box}", fg='red', err=True)
                if binaries_list:
                    click.echo("\nAvailable binaries:")
                    for name in binaries_list:
                        click.echo(f"  - {name}")
                ctx.exit(1)
    except Exception:
        pass  # Continue anyway, the remove endpoint will handle it

    # Confirmation
    if not yes:
        click.echo(f"\nYou are about to remove:")
        click.echo(f"  Binary: {binary_name}")
        click.echo(f"  From:   {box} ({resolved_ip})")
        click.echo()

        if not click.confirm("Proceed?", default=False):
            click.echo("Cancelled.")
            return

    # Remove via HTTP
    url = f'http://{resolved_ip}:5000/binaries/remove'
    try:
        response = requests.post(url, json={'name': binary_name}, timeout=30)

        if response.status_code == 200:
            click.secho(f"Binary '{binary_name}' removed from {box}", fg='green')
        else:
            error_msg = response.json().get('error', response.text)
            click.secho(f"Error removing binary: {error_msg}", fg='red', err=True)
            ctx.exit(1)

    except requests.exceptions.ConnectionError:
        click.secho(f"Error: Could not connect to box at {resolved_ip}", fg='red', err=True)
        click.secho("Possible causes:", err=True)
        click.secho("  - Box is offline or unreachable", err=True)
        click.secho("  - Lager service not running (port 5000)", err=True)
        click.secho("  - Firewall blocking connection", err=True)
        click.secho(f"Try: lager hello --box {box}", err=True)
        ctx.exit(1)
    except requests.exceptions.Timeout:
        click.secho("Error: Request timed out", fg='red', err=True)
        ctx.exit(1)
    except Exception as e:
        click.secho(f"Error: {e}", fg='red', err=True)
        ctx.exit(1)
