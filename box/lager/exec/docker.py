# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.exec.docker - Docker Container Management

Utilities for interacting with Docker containers, including:
- Executing commands inside containers
- Getting container metadata (PID, IP)
- Managing container lifecycle

Migrated from gateway/controller/controller/application/views/run.py (legacy, removed)
"""

import subprocess
import time
import logging

logger = logging.getLogger(__name__)

# Container constants
CONTAINER_NAME = 'lager'
PIGPIO_CONTAINER_NAME = 'pigpio'
LAGER_NETWORK_NAME = 'lagernet'


def is_container_running(container_name=CONTAINER_NAME):
    """
    Check whether a container is running.

    Args:
        container_name: Name of the container to check (default: 'lager')

    Returns:
        bool: True if container is running, False otherwise
    """
    cmd = ['/usr/bin/docker', 'container', 'inspect', container_name]
    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except subprocess.CalledProcessError:
        return False
    return True


def get_container_ip(container_name, network_name=LAGER_NETWORK_NAME):
    """
    Get the IP address of a container on a specific network.

    Args:
        container_name: Name of the container
        network_name: Docker network name (default: 'lagernet')

    Returns:
        str: IP address of the container, or empty string if not found
    """
    base_command = [
        '/usr/bin/docker',
        'inspect',
        f'--format={{{{ .NetworkSettings.Networks.{network_name}.IPAddress }}}}',
        container_name,
    ]
    result = subprocess.run(
        base_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.stdout.decode().strip()


def get_container_pid(proc, container_name=CONTAINER_NAME, max_tries=50):
    """
    Get the PID of a running container.

    Args:
        proc: The subprocess.Popen object for the docker run command
        container_name: Name of the container (default: 'lager')
        max_tries: Maximum number of attempts to get the PID (default: 50)

    Returns:
        int or None: Container PID, or None if not found/container not running
    """
    tries = 0
    while tries < max_tries:
        tries += 1
        if proc.returncode is not None:
            # docker run command has finished, so there's no pid
            return None
        pidproc = subprocess.run(
            ['/usr/bin/docker', 'inspect', "--format={{ .State.Pid }}", container_name],
            check=False,
            capture_output=True,
        )
        if pidproc.returncode == 0:
            try:
                pid = int(pidproc.stdout.strip(), 10)
                if pid != 0:
                    return pid
            except Exception as exc:
                logger.exception('Failed to parse pid', exc_info=exc)
        time.sleep(0.05)
    return None


def kill_container_process(container_name, signal):
    """
    Send a signal to a running container.

    Args:
        container_name: Name of the container to kill
        signal: Signal number to send (e.g., signal.SIGTERM)

    Raises:
        subprocess.CalledProcessError: If docker command fails
    """
    base_command = ['/usr/bin/docker', 'kill', f'--signal={signal}', container_name]
    subprocess.run(
        base_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    # Remove the container after killing it
    subprocess.run(
        ['/usr/bin/docker', 'container', 'rm', container_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def kill_by_proc_id(sig, proc_id):
    """
    Kill a process by its lager process ID.

    Searches for processes matching the proc_id and sends the specified signal.

    Args:
        sig: Signal number to send
        proc_id: Lager process ID (UUID) to search for
    """
    proc = subprocess.run(['ps', 'aux'], capture_output=True, check=True)
    lines = proc.stdout.split(b'\n')
    cmd_arg = None

    # Find the command argument for the process
    for line in lines:
        if proc_id in line and b'/usr/bin/timeout' not in line:
            parts = line.split()
            try:
                py_index = parts.index(b'/usr/local/bin/python3')
                cmd_arg = parts[py_index + 1]
                break
            except ValueError:
                pass

    if not cmd_arg:
        return

    # Find and kill the process
    for line in lines:
        if b'/usr/bin/timeout' not in line and b'/usr/bin/docker' not in line:
            if cmd_arg in line:
                pid = int(line.split()[1])
                import os
                os.kill(pid, sig)
                break


def execute_in_container(
    container_name,
    command,
    workdir=None,
    env_vars=None,
    detach=False,
    timeout=None,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT
):
    """
    Execute a command inside a Docker container using docker exec.

    Args:
        container_name: Name of the container
        command: Command to execute (list of strings)
        workdir: Working directory inside container (optional)
        env_vars: Dictionary of environment variables (optional)
        detach: Run in detached mode (default: False)
        timeout: Command timeout in seconds (optional)
        stdin: stdin file descriptor (default: subprocess.PIPE)
        stdout: stdout file descriptor (default: subprocess.PIPE)
        stderr: stderr file descriptor (default: subprocess.STDOUT)

    Returns:
        subprocess.Popen: The running process object
    """
    base_command = []

    # Add timeout if specified
    if timeout and not detach:
        base_command.extend(['/usr/bin/timeout', str(timeout)])

    # Build docker exec command
    base_command.extend(['/usr/bin/docker', 'exec'])

    # Add working directory
    if workdir:
        base_command.extend(['-w', workdir])

    # Add detach flag
    if detach:
        base_command.append('--detach')

    # Add environment variables
    if env_vars:
        for key, value in env_vars.items():
            base_command.append(f'--env={key}={value}')

    # Add container name and command
    base_command.append(container_name)
    base_command.extend(command)

    # Execute the command
    proc = subprocess.Popen(
        base_command,
        stdin=stdin if not detach else subprocess.DEVNULL,
        stdout=stdout if not detach else subprocess.DEVNULL,
        stderr=stderr if not detach else subprocess.DEVNULL,
        bufsize=0,
    )

    return proc
