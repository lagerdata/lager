# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Interactive breakpoints for ``lager python`` scripts.

``lager.pause('label')`` blocks a running script at the call site so a user can
poke at the hardware with ad-hoc ``lager`` CLI commands from another terminal
(a paused script holds no box-wide lock), then resume. Resume happens by
pressing Enter in the foreground ``lager python`` terminal, by running
``lager python --continue <id>`` from anywhere, or automatically after a
timeout so an unattended/CI run never hangs on a forgotten breakpoint.

Coordination is intentionally file-based under ``/tmp/lager_processes/{id}/``
so it never touches the timing-sensitive output-streaming path in
``lager.exec.process``:

    breakpoint.json   written by pause(), read by POST /python/breakpoint
    resume            written by POST /python/continue, polled by pause()

Set ``interactive=True`` to additionally expose a Python prompt (over a socket
on a port in the box's 8081-8090 range) seeded with the caller's locals/globals
for inspecting script state. ``lager python --console <id>`` connects to it.
"""

import os
import sys
import json
import time
import errno
import socket
import inspect
import threading
import contextlib

# Base directory for per-process coordination files. Matches the registry dir
# created for detached processes in lager.python.executor; module-level so unit
# tests can point it at a tmp path.
PROCESS_DIR_BASE = '/tmp/lager_processes'

# Port range the box container already exposes for remote debugging
# (see box/start_box.sh). The interactive console binds the first free one.
CONSOLE_PORT_RANGE = range(8081, 8091)

DEFAULT_TIMEOUT = 300
_POLL_INTERVAL = 0.25
_OFF_VALUES = {'0', 'off', 'false', 'no'}


def _enabled():
    """Breakpoints are on unless LAGER_BREAKPOINTS is an explicit off value."""
    return os.environ.get('LAGER_BREAKPOINTS', '').strip().lower() not in _OFF_VALUES


def _resolve_timeout(timeout):
    """Effective auto-resume timeout: arg > LAGER_BREAKPOINT_TIMEOUT > default.

    A value of 0 (or negative) means block indefinitely until resumed.
    """
    if timeout is None:
        env = os.environ.get('LAGER_BREAKPOINT_TIMEOUT')
        if env is not None:
            try:
                timeout = int(env)
            except ValueError:
                timeout = DEFAULT_TIMEOUT
        else:
            timeout = DEFAULT_TIMEOUT
    return timeout


def _process_dir(process_id):
    return os.path.join(PROCESS_DIR_BASE, process_id)


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


class _SocketConsole:
    """A minimal Python REPL served over a single TCP connection.

    Seeded with the caller's namespace so the user can read/poke script state.
    stdout/stderr/expression-results during evaluation are redirected to the
    socket (the script's real stdout is left alone). Only one console runs at a
    time, in a daemon thread, while the main thread is parked in pause()'s poll
    loop — so the global sys.stdout swap during runcode is safe here.
    """

    def __init__(self, namespace):
        import code  # local import: only needed when interactive
        self._namespace = namespace
        self._server = None
        self._thread = None
        self._stop = threading.Event()
        self._code = code
        self.port = None

    def start(self):
        for port in CONSOLE_PORT_RANGE:
            try:
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(('0.0.0.0', port))
                server.listen(1)
                server.settimeout(0.5)
            except OSError as exc:
                if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                    continue
                raise
            self._server = server
            self.port = port
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()
            return port
        return None

    def stop(self):
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    self._interact(conn)
                except OSError:
                    pass

    def _interact(self, conn):
        rfile = conn.makefile('r', encoding='utf-8', newline='\n')
        wfile = conn.makefile('w', encoding='utf-8', newline='\n')

        def write(data):
            try:
                wfile.write(data)
                wfile.flush()
            except OSError:
                pass

        class _Proxy:
            def write(self, data):
                write(data)
                return len(data)

            def flush(self):
                pass

        proxy = _Proxy()
        # dict copy: console mutations don't leak back into the live script
        console = self._code.InteractiveConsole(dict(self._namespace))

        def runcode(code_obj):
            def _displayhook(value):
                if value is not None:
                    console.locals['_'] = value
                    write(repr(value) + '\n')
            old_hook = sys.displayhook
            sys.displayhook = _displayhook
            try:
                with contextlib.redirect_stdout(proxy), contextlib.redirect_stderr(proxy):
                    self._code.InteractiveInterpreter.runcode(console, code_obj)
            finally:
                sys.displayhook = old_hook

        console.runcode = runcode
        console.raw_input = lambda prompt='': _raw_input(rfile, write, prompt)
        # Route the console's own output (banner, exit message, syntax errors,
        # tracebacks) to the socket too — the default writes to the script's
        # stderr, which would leak the prompt into the `lager python` terminal.
        console.write = write
        try:
            console.interact(
                banner='lager interactive console - inspect script state; Ctrl-D to disconnect',
                exitmsg='disconnected',
            )
        except (EOFError, OSError):
            pass


def _raw_input(rfile, write, prompt):
    write(prompt)
    line = rfile.readline()
    if not line:
        raise EOFError
    return line.rstrip('\n')


def pause(label=None, *, timeout=None, interactive=False):
    """Pause a ``lager python`` script until resumed or a timeout elapses.

    Args:
        label: Optional human-readable tag shown in the banner and breakpoint
            status (e.g. the reason for the breakpoint).
        timeout: Seconds before auto-resuming. Defaults to the
            LAGER_BREAKPOINT_TIMEOUT env var or 300s. 0 blocks until resumed.
        interactive: If True, expose a Python prompt over a socket seeded with
            the caller's locals/globals for inspecting script state.

    Safe to call anywhere: a no-op (with a short notice) when not running under
    ``lager python`` or when LAGER_BREAKPOINTS is set to an off value.
    """
    if not _enabled():
        return

    process_id = os.environ.get('LAGER_PROCESS_ID')
    if not process_id:
        print('lager.pause(): not running under `lager python`; skipping breakpoint',
              file=sys.stderr, flush=True)
        return

    try:
        _run_breakpoint(process_id, label, _resolve_timeout(timeout), interactive)
    except Exception as exc:  # never let a breakpoint crash the user's script
        print(f'lager.pause(): breakpoint error ({exc}); continuing', file=sys.stderr, flush=True)


def _run_breakpoint(process_id, label, timeout, interactive):
    frame = inspect.currentframe().f_back.f_back  # caller of pause()
    filename = frame.f_code.co_filename if frame else '?'
    lineno = frame.f_lineno if frame else 0
    func = frame.f_code.co_name if frame else '?'

    proc_dir = _process_dir(process_id)
    os.makedirs(proc_dir, exist_ok=True)
    state_path = os.path.join(proc_dir, 'breakpoint.json')
    resume_path = os.path.join(proc_dir, 'resume')

    # Clear any stale resume marker so a previous breakpoint can't auto-skip
    # this one.
    _unlink_quiet(resume_path)

    console = None
    console_port = None
    if interactive and frame is not None:
        console = _SocketConsole({**frame.f_globals, **frame.f_locals})
        try:
            console_port = console.start()
        except Exception:
            console_port = None

    state = {
        'paused': True,
        'label': label,
        'file': filename,
        'line': lineno,
        'func': func,
        'since': time.time(),
        'timeout': timeout,
        'pid': os.getpid(),
        'console_port': console_port,
    }
    with open(state_path, 'w') as f:
        json.dump(state, f)

    try:
        _emit_banner(process_id, label, filename, lineno, timeout, console_port)
        _wait(resume_path, timeout)
    finally:
        if console is not None:
            console.stop()
        _unlink_quiet(resume_path)
        _unlink_quiet(state_path)


def _emit_banner(process_id, label, filename, lineno, timeout, console_port):
    box = os.environ.get('LAGER_BOX')
    box_arg = f' --box {box}' if box else ''
    tag = f' "{label}"' if label else ''
    loc = f'{os.path.basename(filename)}:{lineno}'
    auto = f'auto-resume in {timeout}s' if timeout and timeout > 0 else 'no auto-resume'
    lines = [
        f'=== lager breakpoint{tag} at {loc}  (id {process_id})',
        f'    resume: press Enter here, or `lager python --continue {process_id}{box_arg}`',
    ]
    if console_port:
        lines.append(f'    inspect: `lager python --console {process_id}{box_arg}`')
    lines.append(f'    {auto}')
    print('\n'.join(lines), file=sys.stderr, flush=True)


def _wait(resume_path, timeout):
    deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
    while True:
        if os.path.exists(resume_path):
            print('=== resumed', file=sys.stderr, flush=True)
            return
        if deadline is not None and time.monotonic() >= deadline:
            print(f'=== auto-resumed after {timeout}s', file=sys.stderr, flush=True)
            return
        time.sleep(_POLL_INTERVAL)
