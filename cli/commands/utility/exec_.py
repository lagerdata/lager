# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.exec_

    Execute commands in a Docker container locally

    Migrated from cli/exec/commands.py to cli/commands/utility/exec_.py.

    Note: Named exec_ to avoid conflict with Python's exec builtin.
"""
import os
import subprocess
import platform
from pathlib import Path
import click
from ...config import get_devenv_json, write_lager_json, LAGER_CONFIG_FILE_NAME, get_global_config_file_path, devenv_config_list, expand_devenv_path
from ...context.ci_detection import get_ci_environment, is_container_ci
from ...core.param_types import EnvVarType


def _run_command_local(section, path, cmd_to_run, mount, extra_args, debug, interactive, tty, user, group, env, passenv, volumes=()):
    """
    Run a command locally in a Docker container
    """
    full_command = ' '.join((cmd_to_run, *extra_args)).strip()

    image = section.get('image')
    source_dir = os.path.dirname(path)
    mount_dir = section.get('mount_dir')
    working_dir = mount_dir
    repo_root_relative_path = section.get('repo_root_relative_path')
    shell = section.get('shell', '/bin/bash')  # Default to /bin/bash if not specified

    if debug:
        click.echo(f'Command: {full_command}', err=True)

    # Collect LAGER environment variables
    env_vars = [var for var in os.environ if var.startswith('LAGER')]
    env_strings = [f'--env={var}={os.environ[var]}' for var in env_vars]

    base_command = ['docker', 'run', '--rm']
    if interactive:
        base_command.append('-i')
    if tty:
        base_command.append('-t')

    base_command.extend(env_strings)

    # Handle user and group
    user_group_string = ''
    if user:
        user_group_string += user
    if group:
        user_group_string += f':{group}'

    if user_group_string:
        base_command.extend(['-u', user_group_string])

    # Mount global config if it exists
    global_config_path = get_global_config_file_path()
    if os.path.exists(global_config_path):
        base_command.extend([
            '--env=LAGER_CONFIG_FILE_DIR=/lager',
            '-v',
            f'{global_config_path}:/lager/{LAGER_CONFIG_FILE_NAME}'
        ])

    # Handle volume mounting
    if mount:
        base_command.extend([
            '--mount',
            f'source={mount},target={mount_dir}',
        ])
    else:
        if repo_root_relative_path:
            root = Path(os.path.join(source_dir, repo_root_relative_path)).resolve()
            if source_dir.startswith(str(root)):
                trailing = source_dir[len(str(root)):]
                if trailing.startswith('/'):
                    trailing = trailing[1:]
                working_dir = os.path.join(mount_dir, trailing)
            source_dir = root

        base_command.extend(['-v', f'{source_dir}:{mount_dir}'])

    # Handle MAC address if specified
    macaddr = section.get('macaddr')
    if macaddr:
        base_command.append(f'--mac-address={macaddr}')

    # Handle hostname if specified
    hostname = section.get('hostname')
    if hostname:
        base_command.append(f'--hostname={hostname}')

    # Network / platform / ports from config (no CLI flags on exec for these).
    network = section.get('network')
    if network:
        base_command.append(f'--network={network}')
    for port in devenv_config_list(section.get('ports')):
        base_command.extend(['-p', port])
    platform = section.get('platform')
    if platform:
        base_command.extend(['--platform', platform])

    # Custom bind mounts: config-defined first, then any passed on the command line.
    # Specs may use ~, environment variables, and ${PROJECT_ROOT} for portability.
    project_root = os.path.dirname(path)
    for vol in (*devenv_config_list(section.get('volumes')), *volumes):
        base_command.extend(['-v', expand_devenv_path(vol, project_root)])

    # Add custom environment variables: config-defined first, then --env.
    for env_var in (*devenv_config_list(section.get('environment')), *env):
        base_command.append(f'--env={env_var}')

    # Pass through environment variables
    for var_name in passenv:
        if var_name in os.environ:
            base_command.append(f'--env={var_name}={os.environ[var_name]}')

    # Complete the command
    base_command.extend([
        '-w',
        working_dir,
        image,
        shell,
        '-c',
        full_command
    ])

    if debug:
        click.echo(f'Docker command: {" ".join(base_command)}', err=True)

    try:
        proc = subprocess.run(base_command, check=False)
        return proc.returncode
    except FileNotFoundError:
        click.secho("Error: Docker is not installed or not in PATH", fg='red', err=True)
        click.echo("Please install Docker from: https://docs.docker.com/get-docker/", err=True)
        return 1
    except PermissionError:
        click.secho("Error: Permission denied running Docker", fg='red', err=True)
        click.echo("Possible solutions:", err=True)
        click.echo("  - Add your user to the docker group: sudo usermod -aG docker $USER", err=True)
        click.echo("  - Then log out and back in, or run: newgrp docker", err=True)
        return 1
    except Exception as e:
        click.secho(f"Error running Docker: {e}", fg='red', err=True)
        return 1


# Config keys that only apply when we start a container. A .lager is shared between a
# developer's machine and CI, so carrying these is normal and not worth a warning on every
# CI run -- they are reported under --verbose instead.
_DOCKER_ONLY_CONFIG_KEYS = ('network', 'ports', 'platform', 'macaddr', 'hostname',
                            'volumes', 'user', 'group')


def _warn_docker_only_options(section, mount, volumes, user, group, debug):
    """
    Name the options that cannot take effect when the command runs in place.

    Flags passed on the command line are warned about unconditionally: the caller asked
    for something this path cannot do. Config keys are quieter, for the reason above.
    """
    ignored = []
    if mount:
        ignored.append('--mount')
    if volumes:
        ignored.append('--volume')
    if user:
        ignored.append('--user')
    if group:
        ignored.append('--group')

    if ignored:
        click.secho(
            f'Warning: {", ".join(ignored)} ignored: the command runs in the current '
            f'container, so there is nothing to start.',
            fg='yellow', err=True)

    if debug:
        from_config = [key for key in _DOCKER_ONLY_CONFIG_KEYS if section.get(key)]
        if from_config:
            click.echo(
                f'Ignoring container-only .lager keys: {", ".join(from_config)}', err=True)


def _run_command_container(section, cmd_to_run, extra_args, debug, env):
    """
    Run a command in place, without starting a container.

    A container-based CI job already runs inside the devenv image: the shell, the
    toolchain and the checkout are all there. There is also no Docker inside that job
    container, so starting one is not an option even if it were wanted.

    Deliberately does not chdir. The Docker path sets `-w <mount_dir>` because it bind-mounts
    the checkout to that path; here the checkout is wherever the CI system put it, and the
    caller's working directory is already correct.
    """
    # `or`, not a .get default: a .lager may carry an explicit empty shell, and handing
    # that to subprocess.run raises rather than reporting anything useful.
    shell = section.get('shell') or '/bin/bash'
    full_command = ' '.join((cmd_to_run, *extra_args)).strip()

    child_env = os.environ.copy()
    for env_var in (*devenv_config_list(section.get('environment')), *env):
        name, _, value = str(env_var).partition('=')
        child_env[name] = value

    if debug:
        click.echo(f'Command: {full_command}', err=True)
        click.echo(f'Running in the current container ({shell})', err=True)

    try:
        proc = subprocess.run([shell, '-c', full_command], check=False, env=child_env)
        return proc.returncode
    except FileNotFoundError:
        click.secho(f'Error: shell `{shell}` not found in this container', fg='red', err=True)
        click.echo('Set DEVENV `shell` in .lager to a shell the image provides.', err=True)
        return 1
    except PermissionError:
        click.secho(f'Error: permission denied running `{shell}`', fg='red', err=True)
        return 1


@click.command(name='exec', context_settings={"ignore_unknown_options": True},
               short_help="Run a saved command in a local docker container")
@click.pass_context
@click.argument('cmd_name', required=False, metavar='COMMAND')
@click.argument('extra_args', required=False, nargs=-1, metavar='EXTRA_ARGS')
@click.option('--command', help='Raw commandline to execute in docker container', metavar='\'<cmdline>\'')
@click.option('--save-as', default=None, help='Alias under which to save command', metavar='<alias>', show_default=True)
@click.option('--warn/--no-warn', default=True, help='Warn when overwriting saved command', show_default=True)
@click.option(
    '--env',
    multiple=True, type=EnvVarType(), help='Environment variable (FOO=BAR)')
@click.option(
    '--passenv',
    multiple=True, help='Environment variable to inherit from current environment')
@click.option('--mount', '-m', help='Name of volume to mount', required=False)
@click.option('--volume', 'volumes', help='Bind-mount a host path into the container (HOST:CONTAINER[:ro]). Repeatable.', required=False, multiple=True)
@click.option('--interactive/--no-interactive', '-i', is_flag=True, help='Keep STDIN open even if not attached', default=True, show_default=True)
@click.option('--tty/--no-tty', '-t', is_flag=True, help='Allocate a pseudo-TTY', default=True, show_default=True)
@click.option('--user', '-u', help='User to run as in container', default=None)
@click.option('--group', '-g', help='Group to run as in container', default=None)
@click.option('--verbose', '-v', is_flag=True, help='Show verbose output including the full docker command')
def exec_(ctx, cmd_name, extra_args, command, save_as, warn, env, passenv, mount, volumes, interactive, tty, user, group, verbose):
    """
    Execute COMMAND in a docker container locally. COMMAND is a named command which was previously saved using `--save-as`.
    If COMMAND is not provided, execute the command specified by --command. If --save-as is also provided,
    save the command under that name for later use with COMMAND. If EXTRA_ARGS are provided they will be appended
    to the command at runtime
    """
    if not cmd_name and not command:
        click.echo(exec_.get_help(ctx))
        ctx.exit(0)

    # Get DEVENV configuration
    path, data = get_devenv_json()

    if 'DEVENV' not in data:
        click.secho(f'No devenv configuration found in {path}', fg='red', err=True)
        click.echo(f'Please run `lager devenv create` first to set up your development environment.', err=True)
        ctx.exit(1)

    section = data['DEVENV']

    # Captured before the defaulting below, which always resolves a uid/gid: the
    # in-container path warns about what the caller actually asked for, not the default.
    requested_user, requested_group = user, group

    # Precedence: CLI flag wins, then .lager config, then the current uid/gid.
    if user is None:
        user = section.get('user')
    if group is None:
        group = section.get('group')
    if user is None:
        try:
            user = str(os.getuid())
        except AttributeError:
            pass
    if group is None:
        try:
            group = str(os.getgid())
        except AttributeError:
            pass

    # Check for both command name and command string
    if cmd_name and command:
        osname = platform.system()
        if osname == 'Windows':
            msg = 'If the command contains spaces, please wrap it in double quotes e.g. lager exec --command "ls -la"'
        else:
            msg = 'If the command contains spaces, please wrap it in single quotes e.g. lager exec --command \'ls -la\''
        raise click.UsageError(
            f'Cannot specify a command name and a command\n{msg}'
        )

    # Determine command to run
    if cmd_name:
        key = f'cmd.{cmd_name}'
        if key not in section:
            raise click.UsageError(
                f'Command `{cmd_name}` not found.\n'
                f'Run `lager devenv add {cmd_name} "<command>"` to add it.'
            )
        cmd_to_run = section.get(key)
    else:
        cmd_to_run = command
        if save_as:
            # Save the command for future use
            key = f'cmd.{save_as}'
            if key in section and warn:
                click.echo(f'Command `{save_as}` already exists, overwriting.', err=True)
                click.echo(f'Previous value: {section[key]}', err=True)
            section[key] = cmd_to_run
            try:
                write_lager_json(data, path)
            except PermissionError:
                click.secho(f'Error: Permission denied writing to {path}', fg='red', err=True)
                click.echo('Make sure the file is writable.', err=True)
                ctx.exit(1)
            except Exception as e:
                click.secho(f'Error writing to {path}: {e}', fg='red', err=True)
                ctx.exit(1)
            click.echo(f'Saved command: {save_as}')

    debug = ctx.obj.debug or verbose

    # Under container-based CI the job is already running inside the devenv image, so the
    # command runs in place. Starting a container from in there would need a Docker the job
    # container does not have. LAGER_CI_OVERRIDE forces this back to the Docker path.
    if is_container_ci(get_ci_environment()):
        _warn_docker_only_options(section, mount, volumes, requested_user, requested_group, debug)
        # passenv needs no argument here: the child inherits our environment already.
        returncode = _run_command_container(section, cmd_to_run, extra_args, debug, env)
        ctx.exit(returncode)

    # Run the command locally in Docker
    returncode = _run_command_local(
        section, path, cmd_to_run, mount, extra_args,
        debug, interactive, tty, user, group, env, passenv, volumes
    )
    ctx.exit(returncode)
