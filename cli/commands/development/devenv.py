# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Development environment commands.

This module provides CLI commands for managing Docker-based development environments.

Migrated from cli/devenv/commands.py.
"""
import os
import re
import itertools
import subprocess
from pathlib import Path
import click

from ...sort_utils import natural_sort_key
from ...config import (
    read_lager_json,
    write_lager_json,
    get_devenv_json,
    find_devenv_config_path,
    make_config_path,
    LAGER_CONFIG_FILE_NAME,
    get_global_config_file_path,
)


@click.group()
def devenv():
    """
    Manage development environments
    """
    pass


existing_dir_type = click.Path(
    exists=True,
    file_okay=False,
    dir_okay=True,
    readable=True,
    resolve_path=True,
)


def _validate_docker_image_name(image):
    """
    Validate Docker image name format.

    Valid formats:
    - name (e.g., 'ubuntu')
    - name:tag (e.g., 'ubuntu:20.04')
    - registry/name (e.g., 'lagerdata/devenv-cortexm')
    - registry/name:tag (e.g., 'lagerdata/devenv-cortexm:latest')
    """
    import re
    # Docker image name pattern: optional registry, name, optional tag
    # Simplified pattern that covers common cases
    pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9._/-]*[a-zA-Z0-9])?(:[a-zA-Z0-9._-]+)?$'
    if not re.match(pattern, image):
        return False
    # Additional checks
    if image.startswith('/') or image.endswith('/'):
        return False
    if '//' in image:
        return False
    return True


@devenv.command()
@click.pass_context
@click.option('--image', prompt='Docker image', default='lagerdata/devenv-cortexm', show_default=True, help='Docker image name')
@click.option('--mount-dir', prompt='Source code mount directory in docker container',
              default='/app', show_default=True, help='Mount directory path')
@click.option('--shell', help='Shell executable path', default=None)
def create(ctx, image, mount_dir, shell):
    """
    Create development environment
    """
    # Validate Docker image name format
    if not _validate_docker_image_name(image):
        click.secho(f"Error: Invalid Docker image name: '{image}'", fg='red', err=True)
        click.echo("Valid formats:", err=True)
        click.echo("  - name (e.g., 'ubuntu')", err=True)
        click.echo("  - name:tag (e.g., 'ubuntu:20.04')", err=True)
        click.echo("  - registry/name (e.g., 'lagerdata/devenv-cortexm')", err=True)
        click.echo("  - registry/name:tag (e.g., 'lagerdata/devenv-cortexm:latest')", err=True)
        ctx.exit(1)

    if shell is None:
        if image.startswith('lagerdata/'):
            shell = '/bin/bash'
        else:
            shell = click.prompt('Path to shell executable in docker image', default='/bin/bash')

    config_path = find_devenv_config_path()

    if config_path and os.path.exists(config_path):
        data = read_lager_json(config_path)
        if 'DEVENV' in data:
            answer = click.confirm(f'DEVENV section already exists in {config_path}; overwrite?')
            if not answer:
                ctx.exit(0)
    else:
        if config_path is None:
            config_path = make_config_path(os.getcwd())
        data = {}
        # Create the file if it doesn't exist
        Path(config_path).touch()

    data['DEVENV'] = {
        'image': image,
        'mount_dir': mount_dir,
        'shell': shell
    }

    # Handle permission errors gracefully
    try:
        write_lager_json(data, config_path)
    except PermissionError:
        click.secho(f'Error: Permission denied writing to {config_path}', fg='red', err=True)
        click.echo('Make sure the directory is writable.', err=True)
        ctx.exit(1)
    except Exception as e:
        click.secho(f'Error writing to {config_path}: {e}', fg='red', err=True)
        ctx.exit(1)

    click.echo(f'Created devenv config in {config_path}')


@devenv.command()
@click.pass_context
@click.option('--mount', '-m', help='Name of volume to mount', required=False)
@click.option('--user', '-u', help='User to run as', required=False, default=None)
@click.option('--group', '-g', help='Group to run as', required=False, default=None)
@click.option('--name', '-n', help='Set Container name', required=False)
@click.option('--detach/--no-detach', '-d', help='Run container as detached', required=False, default=False, is_flag=True)
@click.option('--port', '-p', help='Do port forwarding', required=False, multiple=True)
@click.option('--entrypoint', help='Container entrypoint', required=False)
@click.option('--network', help='Network mode', required=False)
@click.option('--platform', help='Platform', required=False)
@click.option('--attach', '-a', 'attach_container', help='Attach to a running container by name', required=False, default=None)
@click.option('--shell', '-s', help='Shell to use when attaching (default: config shell or /bin/bash)', required=False, default=None)
def terminal(ctx, mount, user, group, name, detach, port, entrypoint, network, platform, attach_container, shell):
    """
    Start interactive terminal
    """
    path, data = get_devenv_json()
    if 'DEVENV' not in data:
        click.secho(f'No devenv configuration found in {path}', fg='red', err=True)
        click.echo(f'Please run `lager devenv create` first to set up your development environment.', err=True)
        ctx.exit(1)
    devenv_config = data['DEVENV']

    if attach_container:
        shell_cmd = shell or devenv_config.get('shell', '/bin/bash')
        attach_args = ['docker', 'exec', '-it', attach_container, shell_cmd]
        try:
            proc = subprocess.run(attach_args, check=False)
        except FileNotFoundError:
            click.secho("Error: Docker is not installed or not in PATH", fg='red', err=True)
            ctx.exit(1)
        ctx.exit(proc.returncode)

    image = devenv_config.get('image')
    source_dir = os.path.dirname(path)
    mount_dir = devenv_config.get('mount_dir')
    working_dir = mount_dir
    args = [
        'docker',
        'run',
        '-it',
        '--init',
    ]
    ssh_sock = os.getenv('SSH_AUTH_SOCK')
    if ssh_sock:
        args.extend(['-v', f'{ssh_sock}:{ssh_sock}', '-e', f'SSH_AUTH_SOCK={ssh_sock}'])

    keypath = Path(os.getenv('HOME', ''), '.ssh/id_ed25519')
    if keypath.is_file():
        args.extend(['-v', f'{keypath}:/root/.ssh/id_ed25519:ro'])

    known_hosts = Path(os.getenv('HOME', ''), '.ssh/known_hosts')
    if known_hosts.is_file():
        args.extend(['-v', f'{known_hosts}:/root/.ssh/known_hosts:ro'])

    repo_root_relative_path = devenv_config.get('repo_root_relative_path')

    if 'user' in devenv_config:
        user = devenv_config['user']
    if 'group' in devenv_config:
        group = devenv_config['group']
    macaddr = None
    if 'macaddr' in devenv_config:
        macaddr = devenv_config['macaddr']

    hostname = None
    if 'hostname' in devenv_config:
        hostname = devenv_config['hostname']

    if entrypoint:
        args.extend(['--entrypoint', entrypoint])

    if user:
        args.extend(['--user', user])
    if group:
        args.extend(['--group', group])
    if macaddr:
        args.extend(['--mac-address', macaddr])
    if hostname:
        args.extend(['--hostname', hostname])

    if mount:
        args.extend(['--mount', f'source={mount},target={mount_dir}'])
    else:
        if repo_root_relative_path:
            root = Path(os.path.join(source_dir, repo_root_relative_path)).resolve()
            if source_dir.startswith(str(root)):
                trailing = source_dir[len(str(root)):]
                if trailing.startswith('/'):
                    trailing = trailing[1:]
                working_dir = os.path.join(mount_dir, trailing)
            source_dir = root

        args.extend(['-v', f'{source_dir}:{mount_dir}'])

    if name:
        args.extend(['--name', name])

    if network:
        args.append(f'--network={network}')

    args.extend(itertools.chain(*zip(itertools.repeat('-p'), port)))

    if platform:
        args.extend(['--platform', platform])

    if detach:
        args.extend(['-d'])
    else:
        args.extend(['--rm'])

    global_config_path = get_global_config_file_path()
    if os.path.exists(global_config_path):
        args.extend([
            '--env=LAGER_CONFIG_FILE_DIR=/lager',
            '-v',
            f'{global_config_path}:/lager/{LAGER_CONFIG_FILE_NAME}'
        ])

    args.extend(['-w', working_dir])

    args.append(image)

    try:
        proc = subprocess.run(args, check=False)
    except FileNotFoundError:
        click.secho("Error: Docker is not installed or not in PATH", fg='red', err=True)
        click.echo("Please install Docker from: https://docs.docker.com/get-docker/", err=True)
        ctx.exit(1)
    except PermissionError:
        click.secho("Error: Permission denied running Docker", fg='red', err=True)
        click.echo("Possible solutions:", err=True)
        click.echo("  - Add your user to the docker group: sudo usermod -aG docker $USER", err=True)
        click.echo("  - Then log out and back in, or run: newgrp docker", err=True)
        click.echo("  - Or run with sudo (not recommended)", err=True)
        ctx.exit(1)
    except Exception as e:
        click.secho(f"Error running Docker: {e}", fg='red', err=True)
        ctx.exit(1)

    ctx.exit(proc.returncode)


@devenv.command(name='add')
@click.argument('command_name')
@click.argument('command', required=False)
@click.option('--warn/--no-warn', default=True, help='Whether to print a warning if overwriting an existing command.', show_default=True)
def add(command_name, command, warn):
    """
    Add command to devenv
    """
    # Validate command name (only alphanumeric, dashes, and underscores)
    if not re.match(r'^[a-zA-Z0-9_-]+$', command_name):
        click.secho('Error: Command name can only contain letters, numbers, dashes, and underscores', fg='red', err=True)
        click.get_current_context().exit(1)

    path, data = get_devenv_json()
    if 'DEVENV' not in data:
        click.secho(f'No devenv configuration found in {path}', fg='red', err=True)
        click.echo(f'Please run `lager devenv create` first to set up your development environment.', err=True)
        click.get_current_context().exit(1)

    if not command:
        while True:
            command = click.prompt('Please enter the command', default='', show_default=False)
            if command and command.strip():
                break
            click.secho('Error: Command cannot be empty', fg='red', err=True)

    # Validate command is not empty (for when passed as argument)
    if not command or command.strip() == "":
        click.secho('Error: Command cannot be empty', fg='red', err=True)
        click.get_current_context().exit(1)

    key = f'cmd.{command_name}'
    if key in data['DEVENV'] and warn:
        click.echo(f'Command `{command_name}` already exists, overwriting.', err=True)
        click.echo(f'Previous value: {data["DEVENV"][key]}', err=True)

    data['DEVENV'][key] = command

    # Handle permission errors gracefully
    try:
        write_lager_json(data, path)
    except PermissionError:
        click.secho(f'Error: Permission denied writing to {path}', fg='red', err=True)
        click.echo('Make sure the file is writable.', err=True)
        click.get_current_context().exit(1)
    except Exception as e:
        click.secho(f'Error writing to {path}: {e}', fg='red', err=True)
        click.get_current_context().exit(1)

    click.echo(f'Added command: {command_name}')


@devenv.command(name='delete')
@click.argument('command_name')
@click.option('--devenv', '_devenv', help='Devenv name', metavar='foo')
def delete(command_name, _devenv):
    """
    Delete command from devenv
    """
    path, data = get_devenv_json()
    if 'DEVENV' not in data:
        click.secho(f'No devenv configuration found in {path}', fg='red', err=True)
        click.echo(f'Please run `lager devenv create` first to set up your development environment.', err=True)
        click.get_current_context().exit(1)

    key = f'cmd.{command_name}'
    if key not in data['DEVENV']:
        click.secho(f'Command `{command_name}` does not exist', fg='red', err=True)
        click.get_current_context().exit(1)

    del data['DEVENV'][key]

    # Handle permission errors gracefully
    try:
        write_lager_json(data, path)
    except PermissionError:
        click.secho(f'Error: Permission denied writing to {path}', fg='red', err=True)
        click.echo('Make sure the file is writable.', err=True)
        click.get_current_context().exit(1)
    except Exception as e:
        click.secho(f'Error writing to {path}: {e}', fg='red', err=True)
        click.get_current_context().exit(1)

    click.echo(f'Deleted command: {command_name}')


@devenv.command()
def commands():
    """
    List devenv commands
    """
    path, data = get_devenv_json()
    if 'DEVENV' not in data:
        click.secho(f'No devenv configuration found in {path}', fg='red', err=True)
        click.echo(f'Please run `lager devenv create` first to set up your development environment.', err=True)
        click.get_current_context().exit(1)

    devenv_config = data['DEVENV']
    cmds = {k.split('.', 1)[1]: v for k, v in devenv_config.items() if k.startswith('cmd.')}

    if not cmds:
        click.echo('No commands defined')
        return

    for name, command in sorted(cmds.items(), key=lambda x: natural_sort_key(x[0])):
        click.secho(name, fg='green', nl=False)
        click.echo(f': {command}')
