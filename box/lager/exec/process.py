# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.exec.process - Process Output Streaming and Management

Utilities for managing process execution, output streaming, and cleanup.

Migrated from gateway/controller/controller/application/views/run.py (legacy, removed)
"""

import tempfile
import subprocess
import select
import time
import json
import os
import logging
import functools
import threading
import queue as _queue

logger = logging.getLogger(__name__)

KEEPALIVE_TIME = 20  # seconds
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB cap for detached process output logs

# Target pipe buffer size (1 MiB). Linux default is 64 KiB which can stall
# tight-timing scripts (e.g. ROM-bootloader recovery on da14695) if the script
# does any print() while the HTTP socket back to the client is slow to drain.
# Capped by /proc/sys/fs/pipe-max-size at runtime; falls back silently if the
# fcntl call is not permitted.
_TARGET_PIPE_SIZE = 1 * 1024 * 1024

# Drain-queue cap. ~2 MiB of in-flight data per process is plenty of headroom
# for streamed output without letting a runaway script eat unbounded memory.
_DRAIN_QUEUE_MAX = 2048

# fcntl.F_SETPIPE_SZ landed in Linux 2.6.35 but isn't always exposed by the
# Python fcntl module across distros; fall back to the documented constant.
try:
    import fcntl as _fcntl
    _F_SETPIPE_SZ = getattr(_fcntl, 'F_SETPIPE_SZ', 1031)
except Exception:  # pragma: no cover - fcntl is always available on Linux
    _fcntl = None
    _F_SETPIPE_SZ = 1031


def set_pipe_size(fd, size=_TARGET_PIPE_SIZE):
    """
    Try to enlarge a pipe's kernel buffer to ``size`` bytes.

    Silently no-ops on non-Linux, on permission failure, or if the fd is not
    a pipe. This is a *latency* knob — a large pipe buffer keeps the writer
    (the user's script) from ever blocking on stdout/stderr writes when the
    HTTP reader is slow, which would otherwise stretch the response window
    in tight-timing protocols.
    """
    if _fcntl is None or fd is None or fd < 0:
        return
    target = size
    # Cap to the system-wide maximum.
    try:
        with open('/proc/sys/fs/pipe-max-size', 'r') as f:
            target = min(target, int(f.read().strip()))
    except (FileNotFoundError, ValueError, OSError):
        pass
    try:
        _fcntl.fcntl(fd, _F_SETPIPE_SZ, target)
    except (OSError, PermissionError):
        # Not a pipe, or no permission to grow. Either is benign.
        pass


def make_output_channel(cleanup_fns):
    """
    Create a temporary file for output channel.

    The output channel is used to communicate between processes and stream
    additional data beyond stdout/stderr.

    Args:
        cleanup_fns: Set of cleanup functions to add the close function to

    Returns:
        tempfile.NamedTemporaryFile: Output channel file object
    """
    output_channel = tempfile.NamedTemporaryFile('w+b', 0)
    add_cleanup_fn(cleanup_fns, output_channel.close)
    return output_channel


def add_cleanup_fn(cleanup_fns, fn, *args, **kwargs):
    """
    Add a cleanup function to be called later.

    Args:
        cleanup_fns: Set of cleanup functions
        fn: Function to call during cleanup
        *args: Positional arguments for the function
        **kwargs: Keyword arguments for the function

    Returns:
        set: Updated cleanup_fns set
    """
    cleanup_fns.add(functools.partial(fn, *args, **kwargs))
    return cleanup_fns


def do_cleanup(cleanup_fns):
    """
    Execute all cleanup functions.

    Args:
        cleanup_fns: Set of cleanup functions to execute
    """
    for cleanup_fn in cleanup_fns:
        try:
            cleanup_fn()
        except BaseException as exc:
            logger.exception('Cleanup function failed', exc_info=exc)
    cleanup_fns.clear()


def terminate_process(proc):
    """
    Terminate a process gracefully, or kill it if necessary.

    Attempts SIGTERM first, waits up to 2 seconds, then sends SIGKILL if needed.

    Args:
        proc: subprocess.Popen object

    Returns:
        int: Process return code, or -1 if killed
    """
    proc.terminate()
    try:
        return proc.wait(2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(2)
        return -1


def emit(fileno, chunk):
    """
    Emit a chunk of data with a header.

    Format: "<fileno> <length> <chunk>"

    Args:
        fileno: File descriptor number (1=stdout, 2=stderr, 3=output_channel, 0=keepalive)
        chunk: Data chunk to emit

    Yields:
        bytes: Header and chunk data
    """
    header = f'{fileno} {len(chunk)} '.encode()
    yield header
    if chunk:
        yield chunk


def _drain_pipe_to_queue(readable, fileno, q, stop_event):
    """
    Continuously read from a pipe-like fd and push (fileno, chunk) tuples
    into ``q`` until EOF or ``stop_event`` is set. Pushes a final
    (fileno, None) sentinel on EOF so the consumer knows this stream is done.

    This runs on a background thread so that pipe drainage is *independent*
    of how fast the HTTP consumer of the generator pulls bytes — preventing
    a slow client from back-pressuring the user's script, which is critical
    for time-sensitive serial I/O.
    """
    try:
        while not stop_event.is_set():
            chunk = readable.read(1024)
            if chunk == b'':
                break
            try:
                q.put((fileno, chunk), timeout=1.0)
            except _queue.Full:
                # Consumer is unreachable for >1s. Drop on the floor rather
                # than block — never starve the script for the sake of logs.
                logger.warning(
                    'stream_process_output: drain queue full (fileno=%d), dropping %d bytes',
                    fileno, len(chunk),
                )
    except (OSError, ValueError):
        # File closed under us during shutdown — expected.
        pass
    finally:
        try:
            q.put((fileno, None), timeout=1.0)
        except _queue.Full:
            pass


def _drain_file_to_queue(readable, fileno, q, stop_event, proc):
    """
    Variant of _drain_pipe_to_queue for the output_channel tempfile. select()
    treats a regular file as always-readable, so we poll with a small sleep
    when there's nothing new, and stop once the spawned process has exited.
    """
    try:
        while not stop_event.is_set():
            chunk = readable.read(1024)
            if chunk:
                try:
                    q.put((fileno, chunk), timeout=1.0)
                except _queue.Full:
                    logger.warning(
                        'stream_process_output: drain queue full (fileno=%d), dropping %d bytes',
                        fileno, len(chunk),
                    )
                continue
            # No data right now. If process is gone, drain anything remaining
            # one more time and exit.
            if proc.poll() is not None:
                tail = readable.read()
                if tail:
                    try:
                        q.put((fileno, tail), timeout=1.0)
                    except _queue.Full:
                        pass
                break
            time.sleep(0.02)
    except (OSError, ValueError):
        pass
    finally:
        try:
            q.put((fileno, None), timeout=1.0)
        except _queue.Full:
            pass


def stream_process_output(proc, output_channel, cleanup_fns):
    """
    Stream output from a running process.

    Background threads drain stdout/stderr/output_channel into a thread-safe
    queue; the generator yields from the queue. This decouples pipe drainage
    from however quickly the HTTP layer consumes the generator's chunks — a
    slow network or blocked client cannot stretch the user script's syscalls
    by back-pressuring its stdout pipe. That property matters a lot for
    scripts with tight timing budgets (e.g. talking to a ROM bootloader on
    a Dialog/Renesas DA14695 over UART, where responses are due within
    50–120 ms of each received byte).

    Args:
        proc: subprocess.Popen object
        output_channel: Additional output channel file object
        cleanup_fns: Set of cleanup functions to call when done

    Yields:
        bytes: Formatted output chunks with headers

    Format:
        Each chunk is prefixed with: "<fileno> <length> <data>"
        - fileno 0: Keepalive (empty chunk)
        - fileno 1: stdout
        - fileno 2: stderr
        - fileno 3: output_channel
        - Final line: "- <len> <returncode>"
    """
    q = _queue.Queue(maxsize=_DRAIN_QUEUE_MAX)
    stop_event = threading.Event()
    drain_threads = []
    pending_eofs = 0

    def _spawn_pipe_drain(readable, fileno):
        nonlocal pending_eofs
        t = threading.Thread(
            target=_drain_pipe_to_queue,
            args=(readable, fileno, q, stop_event),
            name=f'lager-drain-fd{fileno}',
            daemon=True,
        )
        t.start()
        drain_threads.append(t)
        pending_eofs += 1

    def _spawn_file_drain(readable, fileno):
        nonlocal pending_eofs
        t = threading.Thread(
            target=_drain_file_to_queue,
            args=(readable, fileno, q, stop_event, proc),
            name=f'lager-drain-fd{fileno}',
            daemon=True,
        )
        t.start()
        drain_threads.append(t)
        pending_eofs += 1

    try:
        if proc.stdout is not None:
            _spawn_pipe_drain(proc.stdout, 1)
        if proc.stderr is not None:
            _spawn_pipe_drain(proc.stderr, 2)
        if output_channel is not None:
            _spawn_file_drain(output_channel, 3)

        last_keepalive = time.time()

        while pending_eofs > 0:
            try:
                fileno, chunk = q.get(timeout=0.1)
            except _queue.Empty:
                now = time.time()
                if now - last_keepalive > KEEPALIVE_TIME:
                    last_keepalive = now
                    yield from emit(0, b'')
                continue

            if chunk is None:
                pending_eofs -= 1
                continue

            yield from emit(fileno, chunk)

            now = time.time()
            if now - last_keepalive > KEEPALIVE_TIME:
                last_keepalive = now
                yield from emit(0, b'')

        # All drains have finished. Reap the process and send final code.
        returncode = str(terminate_process(proc))
        yield f'- {len(returncode)} {returncode}'.encode()

    except Exception as exc:
        logger.exception('stream_process_output failed', exc_info=exc)
    finally:
        stop_event.set()
        # Best-effort thread join with a short timeout; drains are daemons
        # so they won't keep the interpreter alive if join times out.
        for t in drain_threads:
            t.join(timeout=0.5)
        do_cleanup(cleanup_fns)


def stream_process_output_to_file(proc, output_channel, cleanup_fns, log_path, meta_path):
    """
    Stream output from a running process into a log file on disk.

    Writes the same wire-format chunks as stream_process_output, but to a file
    instead of yielding them. Used for detached processes so output can be
    replayed later via stream_log_file().

    Args:
        proc: subprocess.Popen object
        output_channel: Additional output channel file object
        cleanup_fns: Set of cleanup functions to call when done
        log_path: Path to the output log file (opened in append-binary mode)
        meta_path: Path to the meta.json file to update on completion
    """
    fileno_map = {
        proc.stdout: 1,
        output_channel: 3,
    }
    if proc.stderr is not None:
        fileno_map[proc.stderr] = 2

    try:
        readables = [proc.stdout, output_channel]
        if proc.stderr is not None:
            readables.append(proc.stderr)

        with open(log_path, 'ab') as log_file:
            cap_reached = False

            while True:
                if readables == [output_channel]:
                    break

                rlist, _wlist, _xlist = select.select(readables, [], [], 0.1)

                for readable in rlist:
                    chunk = readable.read(1024)
                    if chunk == b'':
                        if readable == output_channel:
                            continue
                        readables.remove(readable)

                    if not cap_reached:
                        fileno = fileno_map[readable]
                        for part in emit(fileno, chunk):
                            log_file.write(part)
                        log_file.flush()

                        if log_file.tell() >= MAX_LOG_SIZE:
                            cap_reached = True
                            logger.warning(f"Log file reached {MAX_LOG_SIZE} byte cap, stopping capture")

            returncode = str(terminate_process(proc))

            remaining = output_channel.read()
            if not cap_reached:
                for part in emit(fileno_map[output_channel], remaining):
                    log_file.write(part)

            # Always write the exit marker, even if cap was reached
            exit_marker = f'- {len(returncode)} {returncode}'.encode()
            log_file.write(exit_marker)
            log_file.flush()

        # Update meta.json with finished status
        try:
            with open(meta_path, 'r') as f:
                meta = json.load(f)
            meta['status'] = 'finished'
            meta['returncode'] = int(returncode)
            with open(meta_path, 'w') as f:
                json.dump(meta, f)
        except Exception as exc:
            logger.exception('Failed to update meta.json', exc_info=exc)

    except Exception as exc:
        logger.exception('stream_process_output_to_file failed', exc_info=exc)
    finally:
        do_cleanup(cleanup_fns)


def stream_log_file(log_path, meta_path):
    """
    Stream a log file, tailing it if the process is still running.

    Reads existing data from log_path and yields it in chunks. If the process
    is still running (per meta.json), tails the file for new data. When the
    process finishes and all bytes are read, the generator exhausts.

    Args:
        log_path: Path to the output log file
        meta_path: Path to the meta.json file

    Yields:
        bytes: Chunks of wire-format output data
    """
    with open(log_path, 'rb') as f:
        last_keepalive = time.time()

        while True:
            chunk = f.read(4096)
            if chunk:
                yield chunk
                continue

            # No more data right now — check if process is done
            try:
                with open(meta_path, 'r') as mf:
                    meta = json.load(mf)
                status = meta.get('status', 'running')
            except Exception:
                status = 'running'

            if status == 'finished':
                # Read any final bytes that arrived after last read
                final = f.read()
                if final:
                    yield final
                return

            # Still running — tail the file
            now = time.time()
            if now - last_keepalive > KEEPALIVE_TIME:
                last_keepalive = now
                yield from emit(0, b'')

            time.sleep(0.2)


# Export cleanup functions for external use
cleanup_functions = {
    'add': add_cleanup_fn,
    'do': do_cleanup,
}
