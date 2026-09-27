# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.logs

    Manage box logs
"""
import shlex

import click
from ...core.group_usage import LagerGroup
from ...sort_utils import natural_sort_key
from ...box_storage import empty_box_name_error, list_boxes
from ..box._ssh import default_ssh_runner, resolve_box_user


@click.group(cls=LagerGroup)
def logs():
    """Manage box logs"""
    pass


def _run_on_box(ip, cmd, timeout):
    """(rc, stdout, stderr) for `cmd` run on the box as its saved login user.

    `default_ssh_runner` resolves that user and offers the lager_box key, then
    the user's own identities. These commands used to hardcode `lagerdata@`,
    which fails on a box with any other login user. A timeout comes back as
    rc 255 with a "timed out" message rather than as an exception.
    """
    return default_ssh_runner(ip, cmd, timeout=timeout)


def _timed_out(rc, stderr):
    return rc == 255 and 'timed out' in (stderr or '')


def _saved_box_ip(ctx, box):
    """IP of a saved box, or print the saved boxes and exit."""
    if not box.strip():
        raise empty_box_name_error()
    saved_boxes = list_boxes()
    if box not in saved_boxes:
        click.secho(f"Error: Box '{box}' not found", fg='red', err=True)
        click.echo("Available boxes:", err=True)
        for name in sorted(saved_boxes.keys(), key=natural_sort_key):
            click.echo(f"  - {name}", err=True)
        ctx.exit(1)
    box_info = saved_boxes[box]
    return box_info.get('ip', 'unknown') if isinstance(box_info, dict) else box_info


@logs.command('clean')
@click.pass_context
@click.option('--box', required=True, help='Box to clean logs on')
@click.option('--older-than', default=1, type=int, help='Remove logs older than N days (default: 1)')
@click.option('--yes', is_flag=True, help='Skip confirmation')
def clean(ctx, box, older_than, yes):
    """Clean old log files from box"""
    # Validate older-than is positive
    if older_than < 0:
        click.secho(f"Error: --older-than must be non-negative, got {older_than}", fg='red', err=True)
        ctx.exit(1)

    ip = _saved_box_ip(ctx, box)

    if not yes:
        click.confirm(f"Clean logs older than {older_than} day(s) on {box} ({ip})?", abort=True)

    # Build command to remove old logs
    # Note: -mtime +N means "modified more than N days ago"
    cmd = f'find ~/box/logs -name "*.log" -type f -mtime +{older_than} -delete 2>/dev/null || true'

    click.echo(f'Cleaning logs on {box}...', nl=False)
    rc, _stdout, stderr = _run_on_box(ip, cmd, timeout=30)
    if _timed_out(rc, stderr):
        click.secho(' TIMEOUT', fg='red', err=True)
        click.echo('SSH command timed out after 30 seconds', err=True)
        ctx.exit(1)

    if rc == 0:
        click.secho(' OK', fg='green')

        # Show how much space was freed
        size_cmd = 'du -sh ~/box/logs/ 2>/dev/null || echo "0"'
        rc, stdout, stderr = _run_on_box(ip, size_cmd, timeout=10)
        if rc == 0:
            size = stdout.strip().split()[0] if stdout.strip() else "0"
            click.echo(f'Current logs size: {size}')
        elif _timed_out(rc, stderr):
            click.secho('Warning: the box did not return the current logs size', fg='yellow', err=True)
    else:
        click.secho(' FAILED', fg='red', err=True)
        from ...errors import ssh_error
        ssh_error(stderr, ip, user=resolve_box_user(ip)).die()


@logs.command('size')
@click.pass_context
@click.option('--box', help='Box to check (if not specified, checks all)')
@click.option('--verbose', '-v', is_flag=True, help='Show individual log files')
def size(ctx, box, verbose):
    """Check log file sizes on box(es)"""
    saved_boxes = list_boxes()

    # Filter to specific box if requested. `--box ""` is refused rather than
    # read as "every box".
    if box is not None:
        if not box.strip():
            raise empty_box_name_error()
        if box not in saved_boxes:
            click.secho(f"Error: Box '{box}' not found", fg='red', err=True)
            click.echo("Available boxes:", err=True)
            for name in sorted(saved_boxes.keys(), key=natural_sort_key):
                click.echo(f"  - {name}", err=True)
            ctx.exit(1)
        boxes_to_check = {box: saved_boxes[box]}
    else:
        boxes_to_check = saved_boxes

    # Check each box
    for name, box_info in sorted(boxes_to_check.items(), key=lambda x: natural_sort_key(x[0])):
        if isinstance(box_info, dict):
            ip = box_info.get('ip', 'unknown')
        else:
            ip = box_info

        click.echo(f'\n{name} ({ip}):')

        # Get total size
        size_cmd = 'du -sh ~/box/logs/ 2>/dev/null || echo "0\t(not found)"'
        rc, stdout, stderr = _run_on_box(ip, size_cmd, timeout=10)
        if _timed_out(rc, stderr):
            click.secho('  Connection timed out', fg='red', err=True)
            continue

        if rc == 0:
            size_output = stdout.strip()
            if size_output:
                try:
                    size, path = size_output.split('\t', 1)
                except ValueError:
                    size = size_output
                click.echo(f'  Total logs: {size}')

                # Parse size and warn if too large
                size_str = size.strip()
                if size_str.endswith('M'):
                    try:
                        size_mb = float(size_str[:-1])
                        if size_mb > 500:
                            click.secho('  Warning: Logs are large, consider cleaning', fg='yellow')
                    except ValueError:
                        pass  # Ignore parsing errors
                elif size_str.endswith('G'):
                    click.secho('  ERROR: Logs are very large! Clean immediately:', fg='red')
                    click.echo(f'    lager logs clean --box {name} --yes')
            else:
                click.echo('  Total logs: 0')

            # Show individual files if verbose
            if verbose:
                files_cmd = 'find ~/box/logs -name "*.log" -type f -exec ls -lh {} \\; 2>/dev/null | awk \'{print "    " $9 ": " $5}\''
                rc, stdout, stderr = _run_on_box(ip, files_cmd, timeout=15)
                if rc == 0 and stdout.strip():
                    click.echo('  Individual files:')
                    click.echo(stdout.strip())
                elif _timed_out(rc, stderr):
                    click.secho('  Warning: the box did not return the file list (timed out)', fg='yellow', err=True)
        else:
            # In a per-box loop: show the actionable message but keep going
            # to the next box rather than exiting. Indent to fit the listing.
            from ...errors import ssh_error
            message = ssh_error(stderr, ip, user=resolve_box_user(ip)).format_message()
            click.echo('\n'.join('  ' + line for line in message.splitlines()), err=True)


_NO_SUDO_MARK = 'unknown (reading the log needs sudo, which asked for a password)'

# `__CONTAINERS__` is replaced with one quoted name or the running containers.
_DOCKER_LOG_SIZES_SCRIPT = (
    'for c in __CONTAINERS__; do '
    'echo "Container: $c"; '
    'p=$(docker inspect "$c" --format "{{.LogPath}}") || exit 3; '
    'if [ -z "$p" ]; then echo "  Size: unknown (no log file)"; continue; fi; '
    'if line=$(sudo -n ls -lh "$p" 2>/dev/null); then '
    'echo "$line" | awk \'{print "  Size: " $5 " Path: " $9}\'; '
    'else echo "  Size: ' + _NO_SUDO_MARK + '"; fi; '
    'done'
)


@logs.command('docker')
@click.pass_context
@click.option('--box', required=True, help='Box to check')
@click.option('--container', help='Specific container name (default: all containers)')
def docker_logs(ctx, box, container):
    """Check Docker container log sizes"""
    ip = _saved_box_ip(ctx, box)

    click.echo(f'Docker logs on {box} ({ip}):\n')

    # Docker's json log files are root-only, so reading their size needs
    # sudo. It runs with -n: over BatchMode SSH a password prompt cannot be
    # answered, and a sudo that failed quietly used to print nothing at all.
    # `docker` itself failing (not in the docker group, daemon down) exits 3
    # so the output below names it instead of reporting "no containers".
    cmd = _DOCKER_LOG_SIZES_SCRIPT.replace(
        '__CONTAINERS__',
        shlex.quote(container) if container else '$(docker ps --format "{{.Names}}")',
    )
    if not container:
        cmd = 'docker ps -q >/dev/null || exit 3; ' + cmd

    rc, stdout, stderr = _run_on_box(ip, cmd, timeout=30)
    if _timed_out(rc, stderr):
        click.secho('Error: SSH command timed out', fg='red', err=True)
        ctx.exit(1)

    if rc == 0:
        if stdout.strip():
            click.echo(stdout.strip())
            if _NO_SUDO_MARK in stdout:
                click.secho(
                    f'\nThe log sizes need root. Allow `sudo -n ls` for the login user '
                    f'({resolve_box_user(ip)}) on the box, or read them there with sudo.',
                    fg='yellow', err=True)
            click.echo('\nNote: Docker logs are automatically rotated with max-size=10m, max-file=3')
            click.echo('Total maximum per container: ~30MB')
        else:
            click.echo('No containers found or no logs')
    else:
        click.secho('Failed to check Docker logs', fg='red', err=True)
        if stderr:
            click.echo(stderr, err=True)
        ctx.exit(1)
