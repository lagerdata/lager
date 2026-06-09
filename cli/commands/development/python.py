# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Python script execution commands.

This module provides CLI commands for running Python scripts on Lager boxes.
"""
import os
import shutil
import gzip
import codecs
import json
import uuid
import sys
import atexit
import time
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

# Handle SIGPIPE for pipeline support (e.g., lager python script.py | head)
# When the downstream process in a pipeline closes, we should exit gracefully
if hasattr(signal, 'SIGPIPE'):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

_ORIGINAL_SIGINT_HANDLER = signal.getsignal(signal.SIGINT)


# ---------------------------------------------------------------------------
# Auto-lock plumbing for `lager python`
# ---------------------------------------------------------------------------
# The CLI registers (box_ip, holder) in this dict for the duration of the
# run. Signal handlers, atexit, and the regular finally path all read from it
# so the lock is released no matter how the process dies.
#
# We use a module-level dict (rather than passing state around) because the
# release paths sit far apart (signal handler installed by sigint_handler,
# `_do_exit` for streamed exit, atexit for "everything else").
_AUTO_LOCK_STATE = {
    'active': False,        # True between acquire and the first release attempt
    'released': False,      # True once any path has released the lock
    'box_ip': None,
    'holder': None,
    'box_label': None,      # human-readable name for messages
    'detach': False,        # if True we never release at CLI exit
}


def _auto_lock_release(reason=''):
    """Release the auto-lock if we hold one and haven't released it yet.

    Idempotent. Safe to call from signal handlers, atexit, and finally
    blocks. Skipped entirely for ``--detach`` runs (the lock is intentionally
    retained for the detached process).
    """
    if not _AUTO_LOCK_STATE['active'] or _AUTO_LOCK_STATE['released']:
        return
    if _AUTO_LOCK_STATE['detach']:
        return
    from ...box_storage import release_box_lock
    _AUTO_LOCK_STATE['released'] = True
    try:
        release_box_lock(_AUTO_LOCK_STATE['box_ip'], _AUTO_LOCK_STATE['holder'])
    except Exception:  # pylint: disable=broad-except
        pass


# atexit fires for "normal" interpreter exits and for sys.exit(); it does NOT
# fire for os._exit or for fatal signals that aren't caught. Those paths are
# covered by the explicit signal handler in sigint_handler and the box-side
# TTL/heartbeat reap.
atexit.register(_auto_lock_release, 'atexit')


class _HeartbeatThread(threading.Thread):
    """Refreshes the box lock periodically while a test is running.

    Daemon thread so it dies with the CLI process. Stop by calling .stop();
    that also wakes the sleep so shutdown is prompt.

    Heartbeat failures are logged once (to stderr, lowercase warning) and
    then retried silently. We do NOT abort the test on heartbeat failure —
    the server-side TTL is the authoritative reaper, and treating a flaky
    network as a test failure would generate more flake than it prevents.
    """

    def __init__(self, ip, holder, interval):
        super().__init__(daemon=True, name='lager-lock-heartbeat')
        self._ip = ip
        self._holder = holder
        self._interval = max(1, int(interval))
        # NOTE: must NOT be named ``_stop`` — ``threading.Thread`` itself
        # uses ``self._stop`` as a method during teardown, and assigning an
        # Event there raises ``TypeError: 'Event' object is not callable``
        # when the thread finishes normally.
        self._stop_event = threading.Event()
        self._warned = False

    def stop(self):
        self._stop_event.set()

    def run(self):
        from ...box_storage import heartbeat_box_lock
        while not self._stop_event.wait(self._interval):
            try:
                ok = heartbeat_box_lock(self._ip, self._holder)
            except Exception:  # pylint: disable=broad-except
                ok = False
            if not ok and not self._warned:
                self._warned = True
                try:
                    click.secho(
                        'Warning: lock heartbeat failed; relying on server TTL.',
                        fg='yellow', err=True,
                    )
                except Exception:  # pylint: disable=broad-except
                    pass


def sigint_handler(kill_python, box_ip, _sig, _frame):
    """
    Handle Ctrl+C by restoring the old signal handler (so that subsequent
    Ctrl+C will actually stop python) and sending SIGTERM to the running
    docker container. Also releases the auto-acquired box lock so other
    callers don't have to wait for the TTL to reap it.

    Note: prior versions also POSTed /cache/clear to hardware_service here
    (the v0.16.5 band-aid). v0.16.8 routes all hardware access through a
    single shared pyvisa session per VISA address that's *meant* to persist
    across CLI calls; clearing it on every script exit defeated that and
    re-introduced the libusb release-interface race that surfaced as
    [Errno 16] Resource busy on the next open. Removed.
    """
    click.echo(' Attempting to stop Lager Python job')
    signal.signal(signal.SIGINT, _ORIGINAL_SIGINT_HANDLER)
    try:
        kill_python(signal.SIGTERM)
    finally:
        _auto_lock_release('sigint')


def _do_exit(exit_code, box, session, downloads):
    if exit_code == FAILED_TO_RETRIEVE_EXIT_CODE:
        click.secho('Failed to retrieve script exit code.', fg='red', err=True)
    elif exit_code == SIGTERM_EXIT_CODE:
        click.secho('Script terminated due to timeout.', fg='red', err=True)
    elif exit_code == SIGKILL_EXIT_CODE:
        click.secho('Script forcibly killed due to timeout.', fg='red', err=True)

    # Release the auto-lock before sys.exit, so the lock is freed
    # synchronously rather than relying on atexit running on the way out.
    _auto_lock_release('do_exit')

    # Note: prior versions POSTed /cache/clear to hardware_service here.
    # v0.16.8 owns one persistent pyvisa session per VISA address inside
    # hardware_service and shares it across CLI/TUI/script callers, so
    # clearing it on every script exit was actively harmful — it tore down
    # the session another caller might still need and forced a re-open
    # that often raced with libusb's async release-interface (surfaced as
    # [Errno 16] Resource busy). Removed.

    for filename in downloads:
        try:
            with session.download_file(box, filename) as resp:
                # Check for HTTP errors
                if resp.status_code >= 400:
                    if resp.status_code == 404:
                        click.secho(f'Failed to download {filename}: File not found', fg='red', err=True)
                    else:
                        try:
                            error_msg = resp.json().get('error', resp.text)
                        except:
                            error_msg = resp.text
                        click.secho(f'Failed to download {filename}: HTTP {resp.status_code} - {error_msg}', fg='red', err=True)
                    continue

                basename = os.path.basename(filename)
                # DirectHTTPSession returns raw files, backend returns gzipped
                # Detect format by checking magic bytes (gzip starts with 0x1f8b)
                content = resp.content

                # Check if content is gzipped by looking at first 2 bytes
                is_gzipped = len(content) >= 2 and content[0] == 0x1f and content[1] == 0x8b

                with open(basename, 'wb') as f_out:
                    if is_gzipped:
                        # Decompress gzipped content
                        f_out.write(gzip.decompress(content))
                    else:
                        # Write raw content
                        f_out.write(content)
        except requests.HTTPError as exc:
            if hasattr(exc, 'response') and exc.response.status_code == 404:
                click.secho(f'Failed to download {filename}: File not found', fg='red', err=True)
            else:
                click.secho(f'Failed to download {filename}: {exc}', fg='red', err=True)
        except Exception as exc:  # pylint: disable=broad-except
            click.secho(f'Failed to download {filename}: {exc}', fg='red', err=True)
    sys.exit(exit_code)


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


_SIGNAL_MAP = {
    'SIGINT': 2,
    'SIGQUIT': 3,
    'SIGABRT': 6,
    'SIGKILL': 9,
    'SIGUSR1': 10,
    'SIGUSR2': 12,
    'SIGTERM': 15,
    'SIGSTOP': 19,
}

_SIGNAL_CHOICES = click.Choice(list(_SIGNAL_MAP.keys()), case_sensitive=False)


def _get_signal_number(name):
    return _SIGNAL_MAP[name.upper()]


def collect_output_callback(datatype, content, context):
    if context is None:
        context = b''
    if datatype == StreamDatatypes.EXIT:
        return (True, context)
    elif datatype == StreamDatatypes.STDOUT:
        context += content
    elif datatype == StreamDatatypes.STDERR:
        click.echo(content.decode("utf-8", errors="ignore"), nl=False, err=True)
    elif datatype == StreamDatatypes.OUTPUT:
        click.echo(content)
    return False, context


def run_python_internal_get_output(ctx, runnable, box, env, passenv, kill, download, allow_overwrite, signum, timeout, detach, port, org, args, extra_files=None):
    return run_python_internal(ctx, runnable, box, env, passenv, kill, download, allow_overwrite, signum, timeout, detach, port, org, args, extra_files=None, callback=collect_output_callback)


def run_python_internal(ctx, runnable, box, env, passenv, kill, download, allow_overwrite, signum, timeout, detach, port, org, args, extra_files=None, callback=None, dut_name=None, watch_stdin_resume=True):
    if extra_files is None:
        extra_files = []

    # Use appropriate session based on whether box is an IP address
    box_ip = box
    if box_ip is None:
        box_ip = get_default_box(ctx)

    # Auto-detect dut_name if not provided
    # This allows username lookup to work even when commands don't explicitly pass dut_name
    if dut_name is None and box_ip:
        from ...box_storage import get_box_ip, get_box_name_by_ip

        # Try forward lookup: is box_ip a box name?
        resolved_ip = get_box_ip(box_ip)
        if resolved_ip:
            dut_name = box_ip  # Preserve the box name for username lookup
            box_ip = resolved_ip  # Use the IP for the session
        else:
            # Try reverse lookup: is box_ip an IP with a saved box name?
            dut_name = get_box_name_by_ip(box_ip)
            # If found, dut_name is set; if not, it stays None (will use default username)

    session = ctx.obj.get_session_for_box(box_ip, box_name=dut_name)

    if kill:
        signum = _get_signal_number(signum)
        resp = session.kill_python(box_ip, None, signum)
        resp.raise_for_status()
        return

    # Note: debug_tunnel (cloud PDB tunneling) was removed in open source version
    # Remote debugging now uses direct SSH connections instead

    post_data = [
        ('stdout_is_stderr', stdout_is_stderr()),
        ('detach', '1' if detach else '0'),
    ]
    if org:
        post_data.append(('org', org))

    post_data.extend(
        zip(itertools.repeat('args'), args)
    )
    post_data.extend(
        zip(itertools.repeat('env'), env)
    )
    lager_process_id = str(uuid.uuid4())
    post_data.append(('env', f'LAGER_PROCESS_ID={lager_process_id}'))
    post_data.append(('env', f'LAGER_RUNNABLE={runnable}'))
    post_data.extend(
        zip(itertools.repeat('env'), [f'{name}={os.environ[name]}' for name in passenv])
    )
    post_data.extend(
        zip(itertools.repeat('portforwards'), [json.dumps(p._asdict()) for p in port])
    )

    if timeout is not None:
        post_data.append(('timeout', timeout))

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

    try:
        resp = session.run_python(box_ip, files=post_data)
    except requests.exceptions.Timeout:
        click.secho(f'Error: Connection to box timed out ({box_ip})', fg='red', err=True)
        click.secho('The box may be overloaded or unreachable.', err=True)
        ctx.exit(1)
    except requests.exceptions.ConnectionError as e:
        error_str = str(e).lower()
        if 'connection refused' in error_str:
            click.secho(f'Error: Connection refused by box ({box_ip})', fg='red', err=True)
            click.secho('The box service may not be running.', err=True)
            click.secho('Check that the Docker container is running: ssh lagerdata@{box_ip} "docker ps"', err=True)
        elif 'no route to host' in error_str or 'network is unreachable' in error_str:
            click.secho(f'Error: No route to host ({box_ip})', fg='red', err=True)
            click.secho('Check your network connection and that Tailscale/VPN is connected.', err=True)
        elif 'name or service not known' in error_str or 'nodename nor servname' in error_str:
            click.secho(f'Error: Could not resolve hostname ({box_ip})', fg='red', err=True)
            click.secho('Check the box name or IP address is correct.', err=True)
        else:
            click.secho(f'Error: Could not connect to box ({box_ip})', fg='red', err=True)
            click.secho(f'Details: {e}', err=True)
        ctx.exit(1)
    except requests.exceptions.RequestException as e:
        click.secho(f'Error: HTTP request failed: {e}', fg='red', err=True)
        ctx.exit(1)

    # Check for HTTP errors before trying to parse streaming response
    if resp.status_code >= 400:
        if resp.status_code == 401:
            click.secho('Error: Authentication failed (HTTP 401)', fg='red', err=True)
            click.secho('Check box connectivity with: lager hello', err=True)
        elif resp.status_code == 403:
            click.secho('Error: Access forbidden (HTTP 403)', fg='red', err=True)
            click.secho('You may not have permission to access this box.', err=True)
        elif resp.status_code == 404:
            click.secho('Error: Resource not found (HTTP 404)', fg='red', err=True)
            click.secho('The box endpoint may not be available. Check that the box is properly set up.', err=True)
        elif resp.status_code == 500:
            click.secho('Error: Internal server error on box (HTTP 500)', fg='red', err=True)
            click.secho('Check box logs with: lager logs --box [BOX_NAME]', err=True)
        elif resp.status_code == 502:
            click.secho('Error: Bad gateway (HTTP 502)', fg='red', err=True)
            click.secho('The box service may be restarting. Try again in a few seconds.', err=True)
        elif resp.status_code == 503:
            click.secho('Error: Service unavailable (HTTP 503)', fg='red', err=True)
            click.secho('The box service is temporarily unavailable. Try again later.', err=True)
        else:
            click.secho(f'Error: Box returned HTTP {resp.status_code}', fg='red', err=True)
            try:
                # Try to extract error message from JSON response
                error_data = resp.json()
                if 'error' in error_data:
                    click.secho(f'Details: {error_data["error"]}', err=True)
                else:
                    click.echo(resp.text, err=True)
            except Exception:
                if resp.text:
                    click.echo(resp.text, err=True)
        ctx.exit(1)

    # Handle detached mode: parse JSON response and return immediately
    if detach:
        try:
            data = resp.json()
            process_id = data.get('lager_process_id', lager_process_id)
            box_label = dut_name or box_ip
            click.echo(f'Process detached (Process ID: {process_id})')
            click.echo(f'To reattach: lager python --reattach {process_id} --box {box_label}')
            click.echo(f'To kill: lager python --kill {process_id} --box {box_label}')
        except Exception:
            click.echo('Process detached.')
        return

    kill_python = functools.partial(session.kill_python, box_ip, lager_process_id)
    handler = functools.partial(sigint_handler, kill_python, box_ip)
    signal.signal(signal.SIGINT, handler)

    # Let the user resume a lager.pause() breakpoint by pressing Enter. The box
    # prints the breakpoint banner to the streamed stderr; this just turns a
    # local keypress into a resume request.
    if _should_watch_stdin_for_resume(watch_stdin_resume, callback):
        threading.Thread(
            target=_watch_stdin_for_resume,
            args=(session, box_ip, lager_process_id),
            daemon=True,
        ).start()

    try:
        done = False
        context = None
        for (datatype, content) in stream_python_output(resp):
            if callback:
                done, context = callback(datatype, content, context)
                if done:
                    return context
            else:
                if datatype == StreamDatatypes.EXIT:
                    _do_exit(content, box_ip, session, download)
                elif datatype == StreamDatatypes.STDOUT:
                    click.echo(content.decode("utf-8", errors="ignore"), nl=False)
                elif datatype == StreamDatatypes.STDERR:
                    click.echo(content.decode("utf-8", errors="ignore"), nl=False, err=True)
                elif datatype == StreamDatatypes.OUTPUT:
                    click.echo(content)

    except BrokenPipeError:
        # Pipeline downstream closed (e.g., lager python script.py | head).
        # Kill the remote process and exit. (See sigint_handler for why we
        # no longer POST /cache/clear here in v0.16.8.)
        kill_python(signal.SIGTERM)
        sys.exit(0)
    except OutputFormatNotSupported:
        click.secho('Response format not supported. Please upgrade lager-cli', fg='red', err=True)
        sys.exit(1)


def _should_watch_stdin_for_resume(watch_stdin_resume, callback):
    """Whether to spawn the Enter-to-resume stdin watcher for this run.

    The watcher is a daemon thread that blocks on ``sys.stdin.readline()`` with
    no shutdown path, so it must only start for a genuine interactive foreground
    run; otherwise it lingers and races whoever reads stdin next (a Textual TUI,
    a ``click.confirm`` prompt) for keystrokes.

    Gates:
      - ``watch_stdin_resume``: explicit caller opt-out (e.g. the Textual TUIs).
      - ``callback is None``:   streaming runs only; capture runs set a callback.
      - stdin is a tty:         a human is actually at the keyboard.
      - stdout not swapped out:  capture call sites wrap the run in
        ``redirect_stdout(StringIO())``, which reassigns ``sys.stdout``.
        Suppressing the watcher whenever stdout has been redirected stops those
        callers (``lager supply/battery/arm`` net validation, the power TUIs,
        webcam/debug net listing) from leaking a reader that steals input from a
        later TUI or confirm prompt. Piping stdout to another process does *not*
        reassign ``sys.stdout``, so Enter-to-resume still works for e.g.
        ``lager python script.py | tee``.
    """
    return (
        watch_stdin_resume
        and callback is None
        and sys.stdin.isatty()
        and sys.stdout is sys.__stdout__
    )


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
    """
    Reattach to a detached process and stream its output.

    Ctrl+C kills the process (same as normal lager python).
    Ctrl+D detaches from the stream without killing the process.
    """
    try:
        resp = session.attach_python(box_ip, process_id)
    except requests.exceptions.ConnectionError:
        click.secho(f'Could not connect to box at {box_ip}', fg='red', err=True)
        ctx.exit(1)
    except requests.exceptions.Timeout:
        click.secho(f'Connection to box at {box_ip} timed out', fg='red', err=True)
        ctx.exit(1)

    if resp.status_code == 404 or resp.status_code == 422:
        try:
            error_data = resp.json()
            click.secho(error_data.get('error', 'Process not found'), fg='red', err=True)
        except Exception:
            click.secho(f'Process not found: {process_id}', fg='red', err=True)
        ctx.exit(1)
    elif resp.status_code >= 400:
        click.secho(f'Error: Box returned HTTP {resp.status_code}', fg='red', err=True)
        ctx.exit(1)

    # Ctrl+C = kill the process (same as normal lager python)
    kill_python = functools.partial(session.kill_python, box_ip, process_id)
    handler = functools.partial(sigint_handler, kill_python, box_ip)
    signal.signal(signal.SIGINT, handler)

    # Ctrl+D = detach (stdin EOF watcher thread)
    detached_by_user = False

    def watch_stdin_for_detach():
        nonlocal detached_by_user
        try:
            while True:
                line = sys.stdin.readline()
                if not line:  # EOF (Ctrl+D)
                    detached_by_user = True
                    click.echo('\nDetaching...')
                    resp.close()
                    return
                # Enter resumes a script paused at a breakpoint (no-op otherwise)
                try:
                    session.continue_python(box_ip, process_id)
                except Exception:  # pylint: disable=broad-except
                    pass
        except (ValueError, OSError):
            return

    if sys.stdin.isatty():
        stdin_thread = threading.Thread(target=watch_stdin_for_detach, daemon=True)
        stdin_thread.start()

    try:
        for (datatype, content) in stream_python_output(resp):
            if datatype == StreamDatatypes.EXIT:
                signal.signal(signal.SIGINT, _ORIGINAL_SIGINT_HANDLER)
                click.echo(f'Process exited with code {content}')
                sys.exit(content)
            elif datatype == StreamDatatypes.STDOUT:
                click.echo(content.decode("utf-8", errors="ignore"), nl=False)
            elif datatype == StreamDatatypes.STDERR:
                click.echo(content.decode("utf-8", errors="ignore"), nl=False, err=True)
            elif datatype == StreamDatatypes.OUTPUT:
                click.echo(content)
    except (BrokenPipeError, requests.exceptions.ChunkedEncodingError):
        pass
    except OutputFormatNotSupported:
        click.secho('Response format not supported. Please upgrade lager-cli', fg='red', err=True)
        sys.exit(1)
    finally:
        signal.signal(signal.SIGINT, _ORIGINAL_SIGINT_HANDLER)

    if detached_by_user:
        box_label = dut_name or box_ip
        click.echo(f'To reattach: lager python --reattach {process_id} --box {box_label}')
        click.echo(f'To kill: lager python --kill {process_id} --box {box_label}')


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
    from ...box_storage import (
        resolve_and_validate_box,
        acquire_box_lock,
        get_lock_holder,
        default_lock_wait_seconds,
        default_lock_ttl_seconds,
        default_heartbeat_interval,
    )

    # --kill, --kill-all, --reattach, --continue, --console are management ops: skip lock check
    skip_lock = bool(kill or kill_all or reattach or continue_ or console)

    # Resolve and validate the box name. We skip the legacy `_check_box_lock`
    # in `resolve_and_validate_box` when auto-locking is going to run anyway,
    # because acquire_box_lock does a richer collision dance (waits in CI).
    auto_lock_enabled = (
        not skip_lock
        and runnable is not None
        and not os.getenv('LAGER_AUTO_LOCK_DISABLE')
    )
    box_name = box
    box_ip = resolve_and_validate_box(
        ctx, box_name, _skip_lock_check=skip_lock or auto_lock_enabled,
    )

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

    box_label = box_name or box_ip
    should_release = False
    heartbeat = None
    holder = None

    if auto_lock_enabled:
        holder = get_lock_holder()
        # --detach acquires with ttl_seconds=None because the heartbeat thread
        # dies with the CLI process; the detached test outlives us and must
        # be unlocked manually via `lager boxes unlock`.
        ttl = None if detach else default_lock_ttl_seconds()
        wait_seconds = default_lock_wait_seconds()
        state, _data = acquire_box_lock(
            box_ip,
            box_name,
            holder,
            holder_type='ephemeral' if detach else 'ci',
            ttl_seconds=ttl,
            wait_seconds=wait_seconds,
        )
        if state == 'acquired':
            _AUTO_LOCK_STATE.update({
                'active': True,
                'released': False,
                'box_ip': box_ip,
                'holder': holder,
                'box_label': box_label,
                'detach': bool(detach),
            })
            should_release = not detach
            if detach:
                click.secho(
                    f"Box '{box_label}' locked for detached run; "
                    f"release with: lager boxes unlock --box {box_label}",
                    fg='yellow', err=True,
                )
            elif ttl is not None:
                heartbeat = _HeartbeatThread(box_ip, holder, default_heartbeat_interval())
                heartbeat.start()
        elif state == 'already_ours':
            # Pre-existing lock from `lager boxes lock` (same holder). Do NOT
            # release on exit so the user's persistent lock survives.
            pass
        # state == 'unreachable' -> no lock taken; the real command will
        # surface the connection failure when it tries to POST the script.

    try:
        run_python_internal(
            ctx, runnable, box_ip, env, passenv, False, download, allow_overwrite,
            signum, timeout, detach, port, org, args, add_file, dut_name=box_name,
        )
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        if should_release:
            _auto_lock_release('python.finally')
