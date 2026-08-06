# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.exec.process - Process Output Streaming and Management

Utilities for managing process execution, output streaming, and cleanup.

Migrated from gateway/controller/controller/application/views/run.py (legacy, removed)
"""

import tempfile
import signal
import subprocess
import select
import time
import json
import os
import logging
import functools
import threading
import queue as _queue

from lager.exec import quiesce

logger = logging.getLogger(__name__)

KEEPALIVE_TIME = 20  # seconds

# Yielded on every idle poll of the drain queue, roughly ten times a second
# while the script is silent. It carries ZERO bytes and is never written to the
# wire: its only job is to hand control back to the HTTP layer often enough
# that it can check whether the client is still there.
#
# Without it the streaming loop only regains control when the script produces
# output or the 20s keepalive fires, so a disconnect during a quiet stretch --
# and our longer tests are quiet for minutes -- went unnoticed for that whole
# time while the script kept driving the bench.
IDLE_TICK = b''

# How long a script gets to run its cleanup after being interrupted, before we
# escalate. This is the budget for anything in a `finally`: de-energising rails,
# parking outputs, releasing the DUT. Both stop paths share it -- the
# /python/kill RPC and the client-disconnect reaper below -- so that the window
# a test is written against cannot depend on which one happened to fire.
#
# It is an *idle* budget, not a total one (see wait_for_cleanup). A fixed total
# cannot be set correctly from here: it is a guess about how long somebody
# else's teardown takes, and every value is wrong for someone. Our own is ~2.4s
# against benches with a handful of instruments; a user with six LabJacks and
# three hubs could reasonably need ten times that, and picking a number large
# enough for them would mean a wedged script holds a shared box for that long.
#
# So this only has to exceed the longest *pause within* a teardown, which is a
# far more stable quantity than its duration. Measured on hardware across six
# runs of back-to-back USB hub operations, the longest stretch in which the
# script registered no progress at all was 1.12s, so this is roughly 4x the
# observed worst case. It was 3.0s until that measurement, which sat below the
# same figure read off the old CPU-only progress signal (4.17s); an end-to-end
# A/B put that combination's failure rate at 3 of 11 interrupted teardowns.
#
# The cost of a larger budget is bounded and no longer a race: a script that is
# genuinely wedged burns this long before escalating, but the quiesce gate (A8)
# means the next job waits rather than grabbing a mid-teardown bench, and
# CLEANUP_MAX_S still caps the total.
CLEANUP_GRACE_S = 5.0

# Absolute ceiling on cleanup, however busy the script looks. The idle budget
# above cannot distinguish a teardown that is genuinely working from a script
# that ignored the interrupt and simply carried on with its test, because from
# outside they both just burn CPU. This bounds that case. It is deliberately
# far above any plausible teardown, so a script that hits it is misbehaving
# rather than slow.
CLEANUP_MAX_S = 60.0

# How long to wait after SIGTERM before SIGKILL. Short on purpose: a script
# that ignored the interrupt is not going to clean up, so this rung only
# exists to let the interpreter finish dying.
TERMINATE_GRACE_S = 2.0
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


def wait_for_cleanup(proc, idle_s=None, max_s=None, poll_s=0.1):
    """
    Wait for an interrupted process to finish cleaning up.

    ``idle_s`` is how long the process may go without visibly doing anything
    before we give up on it -- not how long cleanup may take in total. A
    teardown that keeps working keeps its deadline pushed out, so it can run
    for as long as it needs; one that is wedged on an instrument that stopped
    answering is still cut off after ``idle_s``, because it has stopped
    executing code and no amount of waiting will change that.

    That distinction is the point. A total budget has to be guessed on behalf
    of every user's teardown, and any guess is wrong for someone. An idle
    budget only has to be longer than the longest pause *within* a teardown,
    which is a far more stable quantity: the settles we and everyone else write
    are sub-second.

    ``max_s`` bounds the case this cannot see. A script that ignored the
    interrupt and carried on running its test looks exactly like a busy
    teardown from out here, so without a ceiling it would hold the bench until
    it finished.

    Where progress cannot be measured (no /proc), degrades to a plain
    ``idle_s`` wait, which is what this did before the watchdog existed.

    Args:
        proc: subprocess.Popen object, already signalled.
        idle_s: seconds of no observable progress before giving up.
            Defaults to CLEANUP_GRACE_S, read at call time.
        max_s: absolute ceiling regardless of progress. Defaults to
            CLEANUP_MAX_S, read at call time.
        poll_s: interval between progress samples.

    Returns:
        int: the return code, if the process exited.
        None: if it was still running when we stopped waiting.
    """
    if idle_s is None:
        idle_s = CLEANUP_GRACE_S
    if max_s is None:
        max_s = CLEANUP_MAX_S

    started = time.monotonic()
    hard_deadline = started + max_s
    idle_deadline = started + idle_s
    last = quiesce.progress_snapshot(proc.pid)
    extended = False

    while True:
        try:
            return proc.wait(poll_s)
        except subprocess.TimeoutExpired:
            pass

        now = time.monotonic()
        current = quiesce.progress_snapshot(proc.pid)
        if current is not None and current != last:
            last = current
            if now + idle_s > idle_deadline:
                idle_deadline = now + idle_s
                if not extended and now - started > idle_s / 2:
                    extended = True
                    logger.info(
                        'pid %s is still working %.1fs after the interrupt; '
                        'extending its cleanup window while it makes progress',
                        proc.pid, now - started,
                    )

        if now >= hard_deadline:
            logger.warning(
                'pid %s has been in cleanup for %.0fs (the ceiling) and is '
                'still going; it is not shutting down, so escalating',
                proc.pid, max_s,
            )
            return None
        if now >= idle_deadline:
            logger.info(
                'pid %s stopped making progress %.1fs after the interrupt '
                '(total %.1fs); escalating to SIGTERM',
                proc.pid, idle_s, now - started,
            )
            return None


def terminate_process(proc, cleanup_grace_s=None):
    """
    Stop a process, giving it a chance to clean up first.

    Escalates SIGINT -> SIGTERM -> SIGKILL. Leading with SIGINT is the whole
    point: Python turns it into a KeyboardInterrupt that unwinds the stack, so
    `finally` blocks, context-manager `__exit__`s and `atexit` hooks all run,
    whereas its default SIGTERM disposition kills the interpreter outright and
    skips every one of them. For a HIL script that is the difference between
    the bench being left idle and left live -- de-energising rails, parking
    outputs and releasing the DUT live in exactly those `finally` blocks.

    This matters most on the path that reaches here without anyone asking for
    a kill: the client went away (cancelled CI job, killed CLI, dropped
    network) and the generator's teardown is reaping the child. It is the only
    cleanup mechanism that survives the supervisor being hard-killed, because
    it runs on the box and needs nothing from the client -- so it is the
    backstop for every executor, `lager python` and `d4 test run` alike.

    The child is normally the /usr/bin/timeout wrapper, which forwards the
    signal to the process group it shares with the script. Signalling only the
    wrapper is deliberate: signalling the script as well delivers the
    interrupt twice, and the second one lands inside the `finally` unwinding
    from the first and truncates the cleanup.

    The cleanup window is an idle budget, not a total one: a teardown that
    keeps doing work keeps getting more time (see wait_for_cleanup). Cleanup
    that legitimately takes 20s completes; cleanup wedged on a dead instrument
    is still cut off promptly.

    Args:
        proc: subprocess.Popen object
        cleanup_grace_s: how long the script may go without making progress
            after the interrupt before we escalate. Defaults to
            CLEANUP_GRACE_S, read at call time.

    Returns:
        int: Process return code, or -1 if it had to be killed
    """
    if cleanup_grace_s is None:
        cleanup_grace_s = CLEANUP_GRACE_S

    # Everything below is the window in which the script is still driving the
    # bench despite the client already being gone. Publish it so the next job
    # waits instead of powering rails out from under this one's teardown.
    with quiesce.reaping_job([proc.pid]):
        # No-ops if the child has already exited, which is the common case on
        # the normal path where the drains hit EOF before we get here.
        proc.send_signal(signal.SIGINT)
        returncode = wait_for_cleanup(proc, idle_s=cleanup_grace_s)
        if returncode is not None:
            return returncode

        proc.terminate()
        try:
            return proc.wait(TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            logger.warning('pid %s ignored SIGTERM; sending SIGKILL', proc.pid)

        proc.kill()
        proc.wait(TERMINATE_GRACE_S)
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

    Also yields a zero-length ``IDLE_TICK`` whenever the script is quiet. It is
    not part of the wire format -- consumers must skip it rather than write it
    -- and exists so the HTTP layer can check on the client between chunks.
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
                yield IDLE_TICK
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
        # Reap the child here, not only on the normal path above.
        #
        # When the client disconnects mid-run (Ctrl-C that never reached
        # /python/kill, a killed CLI, a dropped network, a cancelled CI job)
        # the consumer stops iterating and this generator is closed, raising
        # GeneratorExit at the `yield from emit(...)` inside the read loop.
        # That skips the terminate_process() after the loop, and GeneratorExit
        # derives from BaseException so the `except Exception` above does not
        # catch it either -- the child was never signalled. It survived as an
        # orphan, spinning at 100% CPU, holding its device flock and blocking
        # every later run on that instrument until someone killed it by hand.
        #
        # terminate_process() is SIGTERM, wait 2s, then SIGKILL. The poll()
        # guard keeps the normal path from double-terminating an already-reaped
        # child. Non-detached runs are wrapped in /usr/bin/timeout, so the
        # signal reaches the python child through the wrapper; detached runs
        # (start_new_session=True) are meant to outlive the client and are
        # reaped through /python/kill instead.
        try:
            if proc.poll() is None:
                logger.info(
                    'stream ended with the child still running (client likely '
                    'disconnected) - terminating pid %s', proc.pid,
                )
                terminate_process(proc)
        except Exception:
            logger.exception('failed to terminate child process %s', getattr(proc, 'pid', '?'))
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
