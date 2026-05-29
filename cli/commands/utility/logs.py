# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.logs

    Manage box logs
"""
import click
import subprocess
from ...sort_utils import natural_sort_key
from ...box_storage import list_boxes


@click.group()
def logs():
    """Manage box logs"""
    pass


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

    # Resolve box to IP
    saved_boxes = list_boxes()
    if box not in saved_boxes:
        click.secho(f"Error: Box '{box}' not found", fg='red', err=True)
        click.echo("Available boxes:", err=True)
        for name in sorted(saved_boxes.keys(), key=natural_sort_key):
            click.echo(f"  - {name}", err=True)
        ctx.exit(1)

    box_info = saved_boxes[box]
    if isinstance(box_info, dict):
        ip = box_info.get('ip', 'unknown')
    else:
        ip = box_info

    if not yes:
        click.confirm(f"Clean logs older than {older_than} day(s) on {box} ({ip})?", abort=True)

    # Build command to remove old logs
    # Note: -mtime +N means "modified more than N days ago"
    cmd = f'find ~/box/logs -name "*.log" -type f -mtime +{older_than} -delete 2>/dev/null || true'

    click.echo(f'Cleaning logs on {box}...', nl=False)
    try:
        result = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes', f'lagerdata@{ip}', cmd],
            capture_output=True,
            text=True,
            timeout=30  # 30 second timeout for cleanup operation
        )
    except subprocess.TimeoutExpired:
        click.secho(' TIMEOUT', fg='red', err=True)
        click.echo(f'SSH command timed out after 30 seconds', err=True)
        ctx.exit(1)

    if result.returncode == 0:
        click.secho(' OK', fg='green')

        # Show how much space was freed
        size_cmd = 'du -sh ~/box/logs/ 2>/dev/null || echo "0"'
        try:
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes', f'lagerdata@{ip}', size_cmd],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                size = result.stdout.strip().split()[0] if result.stdout.strip() else "0"
                click.echo(f'Current logs size: {size}')
        except subprocess.TimeoutExpired:
            click.secho('Warning: Could not retrieve current logs size', fg='yellow', err=True)
    else:
        click.secho(' FAILED', fg='red', err=True)
        from ...errors import ssh_error
        ssh_error(result.stderr, ip).die()


@logs.command('size')
@click.pass_context
@click.option('--box', help='Box to check (if not specified, checks all)')
@click.option('--verbose', '-v', is_flag=True, help='Show individual log files')
def size(ctx, box, verbose):
    """Check log file sizes on box(es)"""
    saved_boxes = list_boxes()

    # Filter to specific box if requested
    if box:
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
        try:
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes', f'lagerdata@{ip}', size_cmd],
                capture_output=True,
                text=True,
                timeout=10
            )
        except subprocess.TimeoutExpired:
            click.secho('  Connection timed out', fg='red', err=True)
            continue

        if result.returncode == 0:
            size_output = result.stdout.strip()
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
                try:
                    result = subprocess.run(
                        ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes', f'lagerdata@{ip}', files_cmd],
                        capture_output=True,
                        text=True,
                        timeout=15
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        click.echo('  Individual files:')
                        click.echo(result.stdout.strip())
                except subprocess.TimeoutExpired:
                    click.secho('  Warning: Could not retrieve individual file list (timed out)', fg='yellow', err=True)
        else:
            # In a per-box loop: show the actionable message but keep going
            # to the next box rather than exiting. Indent to fit the listing.
            from ...errors import ssh_error
            message = ssh_error(result.stderr, ip).format_message()
            click.echo('\n'.join('  ' + line for line in message.splitlines()), err=True)


@logs.command('docker')
@click.pass_context
@click.option('--box', required=True, help='Box to check')
@click.option('--container', help='Specific container name (default: all containers)')
def docker_logs(ctx, box, container):
    """Check Docker container log sizes"""
    # Resolve box to IP
    saved_boxes = list_boxes()
    if box not in saved_boxes:
        click.secho(f"Error: Box '{box}' not found", fg='red', err=True)
        click.echo("Available boxes:", err=True)
        for name in sorted(saved_boxes.keys(), key=natural_sort_key):
            click.echo(f"  - {name}", err=True)
        ctx.exit(1)

    box_info = saved_boxes[box]
    if isinstance(box_info, dict):
        ip = box_info.get('ip', 'unknown')
    else:
        ip = box_info

    click.echo(f'Docker logs on {box} ({ip}):\n')

    # Get container logs info
    if container:
        cmd = f'docker inspect {container} --format="{{{{.LogPath}}}}" 2>/dev/null | xargs -I{{}} sudo ls -lh "{{}}" 2>/dev/null | awk \'{{print $5 " " $9}}\''
    else:
        cmd = 'for c in $(docker ps --format "{{.Names}}"); do echo "Container: $c"; docker inspect $c --format="{{.LogPath}}" 2>/dev/null | xargs -I{} sudo ls -lh "{}" 2>/dev/null | awk \'{print "  Size: " $5 " Path: " $9}\'; done'

    try:
        result = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes', f'lagerdata@{ip}', cmd],
            capture_output=True,
            text=True,
            timeout=30
        )
    except subprocess.TimeoutExpired:
        click.secho('Error: SSH command timed out', fg='red', err=True)
        ctx.exit(1)

    if result.returncode == 0:
        if result.stdout.strip():
            click.echo(result.stdout.strip())
            click.echo('\nNote: Docker logs are automatically rotated with max-size=10m, max-file=3')
            click.echo('Total maximum per container: ~30MB')
        else:
            click.echo('No containers found or no logs')
    else:
        click.secho('Failed to check Docker logs', fg='red', err=True)
        if result.stderr:
            click.echo(result.stderr, err=True)
        ctx.exit(1)
