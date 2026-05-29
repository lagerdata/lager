# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Python script execution commands.

This module provides CLI commands for running Python scripts on Lager boxes.

The execution engine (multipart assembly, streaming, detach/reattach/kill,
downloads, port-forwarding, SIGINT handling) lives in `_runner.py` and is shared
with `lager rust`. This module supplies only the Python-specific pieces: how a
script/module is packaged for upload, and the breakpoint/console commands that
have no binary equivalent.
"""
import os
import shutil
import gzip
import codecs
import json
import uuid
import sys
import threading
import pathlib
import itertools
import functools
import signal
import select
import socket
import tempfile
import requests
import click
import trio

from .debug.tunnel import serve_tunnel
from ._runner import (
    RunnerSpec,
    run_internal,
    handle_reattach,
    sigint_handler,
    _do_exit,
    _SIGNAL_MAP,
    _SIGNAL_CHOICES,
    _get_signal_number,
    collect_output_callback,
    _ORIGINAL_SIGINT_HANDLER,
)
from ...context import get_default_box
from ...core.utils import (
    stream_python_output, zip_dir, SizeLimitExceeded,
    FAILED_TO_RETRIEVE_EXIT_CODE,
    SIGTERM_EXIT_CODE,
    SIGKILL_EXIT_CODE,
    StreamDatatypes,
    stdout_is_stderr,
)
from ...core.param_types import EnvVarType, PortForwardType
from ...exceptions import OutputFormatNotSupported

MAX_ZIP_SIZE = 20_000_000  # Max size of zipped folder in bytes


def debug_tunnel(ctx, box):
    host = 'localhost'
    port = 5555
    connection_params = ctx.obj.websocket_connection_params(socktype='pdb', gateway_id=box)
    try:
        trio.run(serve_tunnel, host, port, connection_params, None)
    except PermissionError as exc:
        click.secho(str(exc), fg='red', err=True)
        if ctx.obj.debug:
            raise
    except OSError as exc:
        if ctx.obj.debug:
            raise


def _build_python_runnable(ctx, runnable, post_data, extra_files):
    """Append the Python-specific upload field(s) to post_data.

    A lone script is uploaded as `script`; a directory — or a script that pulls
    in extra files or `.lager` includes — is zipped and uploaded as `module`
    (the box runs `main.py` from the extracted folder).
    """
    if extra_files is None:
        extra_files = []

    # Find and read includes from nearest .lager file
    include_dirs = {}
    if os.path.isfile(runnable):
        search_path = os.path.dirname(os.path.abspath(runnable))
    elif os.path.isdir(runnable):
        search_path = os.path.abspath(runnable)
    else:
        search_path = os.getcwd()

    # Search for .lager file starting from runnable location
    from ...config import make_config_path, get_includes_from_config, LAGER_CONFIG_FILE_NAME
    config_search_dir = search_path
    config_file = None
    while True:
        potential_config = make_config_path(config_search_dir)
        if os.path.exists(potential_config):
            config_file = potential_config
            break
        parent = os.path.dirname(config_search_dir)
        if parent == config_search_dir:
            break
        config_search_dir = parent

    if config_file:
        include_dirs = get_includes_from_config(config_file)
        # Validate includes and warn if paths don't exist
        for dest_path, source_path in list(include_dirs.items()):
            if not os.path.exists(source_path):
                click.secho(f'Warning: Include path "{source_path}" (for "{dest_path}") does not exist, skipping', fg='yellow', err=True)
                del include_dirs[dest_path]
            elif not os.path.isdir(source_path):
                click.secho(f'Warning: Include path "{source_path}" (for "{dest_path}") is not a directory, skipping', fg='yellow', err=True)
                del include_dirs[dest_path]

    if os.path.isfile(runnable):
        if extra_files or include_dirs:
            # Single file with extra files or includes: create temp dir, zip everything as a module
            with tempfile.TemporaryDirectory() as temp_dir:
                # Copy the main script to the temp directory as main.py
                # (box expects main.py when running a module)
                temp_script_path = os.path.join(temp_dir, 'main.py')
                shutil.copy2(runnable, temp_script_path)

                try:
                    max_content_size = MAX_ZIP_SIZE * 2
                    zipped_folder = zip_dir(temp_dir, extra_files, max_content_size=max_content_size, include_dirs=include_dirs)
                except SizeLimitExceeded:
                    click.secho(f'Folder content exceeds max size of {max_content_size:,} bytes', err=True, fg='red')
                    ctx.exit(1)

                if len(zipped_folder) > MAX_ZIP_SIZE:
                    click.secho(f'Zipped module content exceeds max size of {MAX_ZIP_SIZE:,} bytes', err=True, fg='red')
                    ctx.exit(1)

                post_data.append(('module', zipped_folder))
        else:
            # Single file without extra files or includes: upload as script (original behavior)
            with open(runnable, 'rb') as f:
                script_content = f.read()
            # Use BytesIO to create a file-like object that can be read multiple times
            import io
            script_file = io.BytesIO(script_content)
            post_data.append(('script', (os.path.basename(runnable), script_file, 'application/octet-stream')))
    elif os.path.isdir(runnable):
        try:
            max_content_size = MAX_ZIP_SIZE * 2
            zipped_folder = zip_dir(runnable, extra_files, max_content_size=max_content_size, include_dirs=include_dirs)
        except SizeLimitExceeded:
            click.secho(f'Folder content exceeds max size of {max_content_size:,} bytes', err=True, fg='red')
            ctx.exit(1)

        if len(zipped_folder) > MAX_ZIP_SIZE:
            click.secho(f'Zipped module content exceeds max size of {MAX_ZIP_SIZE:,} bytes', err=True, fg='red')
            ctx.exit(1)

        post_data.append(('module', zipped_folder))
    else:
        raise ValueError(f'Could not find runnable {runnable}')


_PYTHON_SPEC = RunnerSpec(
    session_method='run_python',
    build_runnable=_build_python_runnable,
    cli_name='python',
    watch_stdin_resume=True,
)


def run_python_internal_get_output(ctx, runnable, box, env, passenv, kill, download, allow_overwrite, signum, timeout, detach, port, org, args, extra_files=None):
    return run_python_internal(ctx, runnable, box, env, passenv, kill, download, allow_overwrite, signum, timeout, detach, port, org, args, extra_files=None, callback=collect_output_callback)


def run_python_internal(ctx, runnable, box, env, passenv, kill, download, allow_overwrite, signum, timeout, detach, port, org, args, extra_files=None, callback=None, dut_name=None):
    return run_internal(_PYTHON_SPEC, ctx, runnable, box, env, passenv, kill, download, allow_overwrite, signum, timeout, detach, port, org, args, extra_files=extra_files, callback=callback, dut_name=dut_name)


def _watch_stdin_for_resume(session, box_ip, process_id):
    """
    Daemon thread: each line typed on stdin (Enter) asks the box to resume a
    script paused at a lager.pause() breakpoint. Harmless no-op when the script
    is not currently paused (the box returns resumed=False).
    """
    try:
        while True:
            line = sys.stdin.readline()
            if not line:  # EOF (Ctrl+D)
                return
            try:
                session.continue_python(box_ip, process_id)
            except Exception:  # pylint: disable=broad-except
                pass
    except (ValueError, OSError):
        return


def _handle_continue(ctx, box_ip, process_id, session, dut_name):
    """Resume a script paused at a breakpoint, by process ID."""
    try:
        resp = session.continue_python(box_ip, process_id)
    except requests.exceptions.ConnectionError:
        click.secho(f'Could not connect to box at {box_ip}', fg='red', err=True)
        ctx.exit(1)
    if resp.status_code >= 400:
        click.secho(f'Error: Box returned HTTP {resp.status_code}', fg='red', err=True)
        ctx.exit(1)
    if resp.json().get('resumed'):
        click.echo(f'Resumed {process_id}')
    else:
        box_label = dut_name or box_ip
        click.secho(f'No paused breakpoint found for {process_id} on {box_label}', fg='yellow', err=True)


def _handle_console(ctx, box_ip, process_id, session):
    """Connect to the interactive Python console of a paused script."""
    try:
        resp = session.breakpoint_status(box_ip, process_id)
    except requests.exceptions.ConnectionError:
        click.secho(f'Could not connect to box at {box_ip}', fg='red', err=True)
        ctx.exit(1)
    if resp.status_code >= 400:
        click.secho(f'Error: Box returned HTTP {resp.status_code}', fg='red', err=True)
        ctx.exit(1)

    state = resp.json()
    if not state.get('paused'):
        click.secho(f'No script is paused at a breakpoint for {process_id}', fg='yellow', err=True)
        ctx.exit(1)
    port = state.get('console_port')
    if not port:
        click.secho('This breakpoint has no interactive console.', fg='yellow', err=True)
        click.secho('Call pause(..., interactive=True) to enable it.', err=True)
        ctx.exit(1)

    _proxy_console(ctx, box_ip, port)


def _proxy_console(ctx, host, port):
    """Bridge the local terminal to the box's interactive console socket."""
    try:
        sock = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        click.secho(f'Could not connect to console at {host}:{port} ({exc})', fg='red', err=True)
        ctx.exit(1)

    click.echo('Connected to interactive console (Ctrl+D to disconnect)')
    # Incremental decoder buffers partial multibyte chars split across recv chunks.
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    try:
        while True:
            rlist, _w, _x = select.select([sock, sys.stdin], [], [])
            if sock in rlist:
                data = sock.recv(4096)
                if not data:
                    break
                sys.stdout.write(decoder.decode(data))
                sys.stdout.flush()
            if sys.stdin in rlist:
                line = sys.stdin.readline()
                if not line:  # Ctrl+D
                    break
                sock.sendall(line.encode('utf-8'))
    except (OSError, KeyboardInterrupt):
        pass
    finally:
        sock.close()
        click.echo('\nDisconnected from console')


def _handle_reattach(ctx, box_ip, process_id, session, dut_name):
    """Reattach to a detached `lager python` process (see _runner.handle_reattach)."""
    handle_reattach(ctx, box_ip, process_id, session, dut_name, cli_name='python')


@click.command()
@click.pass_context
@click.argument('runnable', required=False, type=click.Path(exists=True))
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option(
    '--env',
    multiple=True, type=EnvVarType(), help='Environment variable (FOO=BAR)')
@click.option(
    '--passenv',
    multiple=True, help='Environment variable to inherit')
@click.option('--kill', default=None, help='Kill a specific process by process ID')
@click.option('--kill-all', is_flag=True, default=False, help='Kill all running scripts')
@click.option('--download', type=click.Path(exists=False, dir_okay=False), multiple=True, help='File to download after completion')
@click.option('--allow-overwrite', is_flag=True, default=False, help='Overwrite existing files when downloading')
@click.option('--signal', 'signum', default='SIGTERM', type=_SIGNAL_CHOICES, help='Signal to use with --kill/--kill-all', show_default=True)
@click.option('--timeout', type=click.IntRange(min=0), default=0, required=False, help='Max runtime in seconds (0=no timeout)')
@click.option('--detach', '-d', is_flag=True, required=False, default=False, help='Detach')
@click.option('--port', '-p', multiple=True, help='Port forwarding (SRC_PORT[:DST_PORT][/PROTOCOL])', type=PortForwardType())
@click.option('--org', default=None, hidden=True)
@click.option('--add-file', type=click.Path(exists=True, dir_okay=False), multiple=True, help='File to upload with script')
@click.option('--reattach', default=None, help='Reattach to detached process by process ID')
@click.option('--continue', 'continue_', default=None, help='Resume a script paused at a breakpoint, by process ID')
@click.option('--console', default=None, help='Connect to the interactive console of a paused script, by process ID')
@click.argument('args', nargs=-1)
def python(ctx, runnable, box, env, passenv, kill, kill_all, download, allow_overwrite, signum, timeout, detach, port, org, add_file, reattach, continue_, console, args):
    """Run Python script on box"""
    from ...box_storage import resolve_and_validate_box

    # --kill, --kill-all, --reattach, --continue, --console are management ops: skip lock check
    skip_lock = bool(kill or kill_all or reattach or continue_ or console)

    # Resolve and validate the box name
    box_name = box
    box_ip = resolve_and_validate_box(ctx, box_name, _skip_lock_check=skip_lock)

    if not runnable and not kill and not kill_all and not reattach and not continue_ and not console:
        raise click.UsageError('Please supply a RUNNABLE, --kill, --kill-all, --reattach, --continue, or --console option')

    if kill:
        session = ctx.obj.get_session_for_box(box_ip, box_name=box_name)
        signum_val = _get_signal_number(signum)
        resp = session.kill_python(box_ip, kill, signum_val)
        if resp.status_code == 422:
            try:
                error_data = resp.json()
                click.secho(error_data.get('error', 'Invalid request'), fg='red', err=True)
            except Exception:
                click.secho(f'Invalid process ID: {kill}', fg='red', err=True)
            ctx.exit(1)
        resp.raise_for_status()
        click.echo(f'Process {kill} killed')
        return

    if kill_all:
        session = ctx.obj.get_session_for_box(box_ip, box_name=box_name)
        signum_val = _get_signal_number(signum)
        resp = session.kill_python(box_ip, None, signum_val)
        resp.raise_for_status()
        click.echo('All processes killed')
        return

    if reattach:
        session = ctx.obj.get_session_for_box(box_ip, box_name=box_name)
        _handle_reattach(ctx, box_ip, reattach, session, box_name)
        return

    if continue_:
        session = ctx.obj.get_session_for_box(box_ip, box_name=box_name)
        _handle_continue(ctx, box_ip, continue_, session, box_name)
        return

    if console:
        session = ctx.obj.get_session_for_box(box_ip, box_name=box_name)
        _handle_console(ctx, box_ip, console, session)
        return

    if not allow_overwrite:
        for filename in download:
            basename = os.path.basename(filename)
            file = pathlib.Path(basename)
            if file.exists():
                raise click.UsageError(f'File {basename} exists; please rename it or use the --allow-overwrite flag')

    # Pass the box name as an environment variable
    env = list(env) if env else []
    if box_name:
        env.append(f'LAGER_BOX={box_name}')

    run_python_internal(ctx, runnable, box_ip, env, passenv, False, download, allow_overwrite, signum, timeout, detach, port, org, args, add_file, dut_name=box_name)
