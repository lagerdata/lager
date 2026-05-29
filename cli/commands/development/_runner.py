# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Shared execution engine for `lager python` and `lager rust`.

Both commands upload a runnable to the box, stream its stdout/stderr/exit code
back using a common framing, and support the same lifecycle operations (detach,
reattach, kill, download, port-forward, SIGINT-to-kill). The only differences
are how the runnable is packaged into the multipart body (a Python script/module
vs a pre-compiled binary) and which box endpoint receives it. Those differences
are captured by a `RunnerSpec`; everything else lives here so the two commands
cannot drift apart.

`python.py` and `rust.py` depend on this module — never the other way around —
to keep the import graph acyclic (this module must not import `.debug.tunnel`).
"""
import os
import gzip
import json
import uuid
import sys
import threading
import itertools
import functools
import signal
import requests
import click

from dataclasses import dataclass
from typing import Callable, Optional

from ...context import get_default_box
from ...core.utils import (
    stream_python_output,
    FAILED_TO_RETRIEVE_EXIT_CODE,
    SIGTERM_EXIT_CODE,
    SIGKILL_EXIT_CODE,
    StreamDatatypes,
    stdout_is_stderr,
)
from ...exceptions import OutputFormatNotSupported

# Handle SIGPIPE for pipeline support (e.g., lager python script.py | head)
# When the downstream process in a pipeline closes, we should exit gracefully
if hasattr(signal, 'SIGPIPE'):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

_ORIGINAL_SIGINT_HANDLER = signal.getsignal(signal.SIGINT)


@dataclass
class RunnerSpec:
    """Describes the language-specific bits of a run.

    session_method: name of the DirectHTTPSession method to POST to
                    ('run_python' -> /python, 'run_exec' -> /exec).
    build_runnable: callback (ctx, runnable, post_data, extra_files) that appends
                    the language-specific upload field(s) to post_data. May call
                    ctx.exit on validation errors.
    cli_name:       'python' | 'rust', used only for the reattach/kill hint text.
    watch_stdin_resume: whether to run the lager.pause() stdin-resume watcher
                    (Python breakpoints only; always False for binaries).
    """
    session_method: str
    build_runnable: Callable
    cli_name: str
    watch_stdin_resume: bool = False


def sigint_handler(kill_python, box_ip, _sig, _frame):
    """
    Handle Ctrl+C by restoring the old signal handler (so that subsequent
    Ctrl+C will actually stop python) and sending SIGTERM to the running
    docker container.

    Note: prior versions also POSTed /cache/clear to hardware_service here
    (the v0.16.5 band-aid). v0.16.8 routes all hardware access through a
    single shared pyvisa session per VISA address that's *meant* to persist
    across CLI calls; clearing it on every script exit defeated that and
    re-introduced the libusb release-interface race that surfaced as
    [Errno 16] Resource busy on the next open. Removed.
    """
    click.echo(' Attempting to stop Lager Python job')
    signal.signal(signal.SIGINT, _ORIGINAL_SIGINT_HANDLER)
    kill_python(signal.SIGTERM)


def _do_exit(exit_code, box, session, downloads):
    if exit_code == FAILED_TO_RETRIEVE_EXIT_CODE:
        click.secho('Failed to retrieve script exit code.', fg='red', err=True)
    elif exit_code == SIGTERM_EXIT_CODE:
        click.secho('Script terminated due to timeout.', fg='red', err=True)
    elif exit_code == SIGKILL_EXIT_CODE:
        click.secho('Script forcibly killed due to timeout.', fg='red', err=True)

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


def run_internal(spec, ctx, runnable, box, env, passenv, kill, download, allow_overwrite,
                 signum, timeout, detach, port, org, args, extra_files=None,
                 callback=None, dut_name=None):
    """Shared engine behind run_python_internal / run_rust_internal."""
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

    # Language-specific: append the script/module or the binary (+companions).
    spec.build_runnable(ctx, runnable, post_data, extra_files)

    try:
        resp = getattr(session, spec.session_method)(box_ip, files=post_data)
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
            click.secho('Check box logs with: lager logs --box <box-name>', err=True)
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
            click.echo(f'To reattach: lager {spec.cli_name} --reattach {process_id} --box {box_label}')
            click.echo(f'To kill: lager {spec.cli_name} --kill {process_id} --box {box_label}')
        except Exception:
            click.echo('Process detached.')
        return

    kill_python = functools.partial(session.kill_python, box_ip, lager_process_id)
    handler = functools.partial(sigint_handler, kill_python, box_ip)
    signal.signal(signal.SIGINT, handler)

    # Let the user resume a lager.pause() breakpoint by pressing Enter. The box
    # prints the breakpoint banner to the streamed stderr; this just turns a
    # local keypress into a resume request. Interactive (human) runs only, and
    # only for languages that support breakpoints (Python, not binaries).
    if spec.watch_stdin_resume and callback is None and sys.stdin.isatty():
        from .python import _watch_stdin_for_resume
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


def handle_reattach(ctx, box_ip, process_id, session, dut_name, cli_name):
    """
    Reattach to a detached process and stream its output.

    Ctrl+C kills the process (same as a normal foreground run).
    Ctrl+D detaches from the stream without killing the process.

    Language-agnostic: it tails the box's per-process log via /python/attach,
    which makes no Python assumptions. `cli_name` only affects the hint text.
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

    # Ctrl+C = kill the process (same as a normal foreground run)
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
        click.echo(f'To reattach: lager {cli_name} --reattach {process_id} --box {box_label}')
        click.echo(f'To kill: lager {cli_name} --kill {process_id} --box {box_label}')
