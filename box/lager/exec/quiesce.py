# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Bench quiesce registry: which jobs are still shutting down.

A job's cleanup runs *after* the thing that asked for it has gone away. The
client is killed (cancelled CI job, Ctrl-C, dropped network), the box notices,
and only then does the script get its SIGINT and start unwinding `finally`
blocks -- de-energising rails, parking outputs, releasing the DUT. For that
whole window the previous job is still driving the bench even though, as far
as every external observer is concerned, it is over.

Nothing used to represent that window. The box lock is released by the
*client* (`release()` in lock_state.py clears unconditionally, and a killed
client's lock lapses via TTL), so the box could hand the bench to the next job
while the previous one was still actuating nets. Two scripts driving the same
instruments is the failure this module exists to prevent.

The registry is the box's answer to "is the bench safe to hand over yet":
whoever reaps a job records its PIDs here for the duration, and whoever starts
a job waits for the registry to clear first. Membership is intersected with
liveness on every read, so a crashed reaper cannot leave a stale entry behind
and there is nothing to garbage-collect.

Scope is deliberately one box, one process: the Python service is a single
`ThreadingHTTPServer`, so every execute, kill and stream handler shares this
module state. Orphans that outlive a service restart are not covered -- those
are what `lager python --kill-all` is for.
"""

import glob
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# How long a job may stay in the registry before we stop believing it is
# shutting down. The escalation that put it here ends in SIGKILL, which is not
# refusable, so a PID still present well past that is wedged in uninterruptible
# sleep -- realistically a USB device that stopped answering. The bench is not
# safe in that state, but neither is blocking the box forever, so we drop the
# claim and let the caller proceed with a warning rather than deadlock CI.
#
# MUST exceed the longest legitimate reap, or the registry drops a job that is
# still being killed and the bench is handed over mid-escalation. That bound is
# CLEANUP_MAX_S + 2 * TERMINATE_GRACE_S from lager.exec.process (64s today), and
# it cannot be imported here -- process.py imports this module, not the other way
# round -- so the relationship is pinned by test_quiesce_bounds_cover_the_reap
# instead. Change either side and that test tells you.
STUCK_AFTER_S = 90.0

POLL_INTERVAL_S = 0.1

_lock = threading.Lock()

# pid -> monotonic timestamp at which reaping began.
_reaping = {}


def pid_is_alive(pid):
    """
    Whether ``pid`` exists and has not already exited.

    Prefers /proc over ``os.kill(pid, 0)`` so that a zombie counts as dead.
    The processes being reaped here are children of this service, and the
    streaming generator reaps them on its own schedule — treating a zombie as
    alive would spend the entire grace window waiting for a process that has
    already run its exit path.

    Falls back to a signal probe where /proc is absent (macOS, for the unit
    tests). The box is Linux, so it takes the zombie-aware path in production.
    """
    try:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            stat = f.read()
    except (ProcessLookupError, PermissionError):
        return False
    except FileNotFoundError:
        if os.path.isdir('/proc'):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    # State is the field after comm, which is parenthesised and may itself
    # contain spaces, so split from the last ')' rather than the first space.
    _, _, rest = stat.rpartition(b')')
    fields = rest.split()
    return bool(fields) and fields[0] != b'Z'


def _stat_fields(pid):
    """Fields of /proc/<pid>/stat after comm, or None if the pid is gone.

    comm is parenthesised and may itself contain spaces and parentheses, so
    everything is taken from the last ')' rather than split on whitespace.
    """
    try:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            stat = f.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    _, _, rest = stat.rpartition(b')')
    fields = rest.split()
    return fields or None


def process_tree(pid):
    """``pid`` and every descendant of it, as a set.

    One pass over /proc building a parent -> children map, so the cost does
    not grow with tree depth. Racy by nature — processes come and go while we
    read — which is fine for the only caller, a progress heuristic.
    """
    children = {}
    for entry in glob.glob('/proc/*/stat'):
        try:
            child = int(entry.split('/')[2])
        except (IndexError, ValueError):
            continue
        fields = _stat_fields(child)
        if not fields or len(fields) < 2:
            continue
        try:
            parent = int(fields[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(child)

    tree = set()
    pending = [pid]
    while pending:
        current = pending.pop()
        if current in tree:
            continue
        tree.add(current)
        pending.extend(children.get(current, ()))
    return tree


def cpu_ticks(pid):
    """utime + stime for ``pid`` in clock ticks, or 0 if it is gone."""
    fields = _stat_fields(pid)
    # stat's first field after comm is state; utime and stime are the 12th and
    # 13th from there (fields 14 and 15 of the whole line, 1-indexed).
    if not fields or len(fields) < 13:
        return 0
    try:
        return int(fields[11]) + int(fields[12])
    except ValueError:
        return 0


def ctxt_switches(pid):
    """
    Voluntary + involuntary context switches for ``pid``, or 0 if it is gone.

    CPU time on its own cannot see a process that is blocked rather than
    computing, and hardware teardown is mostly blocking. Measured on hardware:
    a single Acroname hub round trip takes ~2.2s and accrues about *one* 10ms
    clock tick over that whole time, so sampling ticks at 100ms reads a working
    teardown as idle -- for 4.17s in the worst of six trials, against a 3s
    budget. Every blocking USB transaction does, though, park the process on
    the scheduler and wake it again, so these counters move roughly ten times
    more often than the tick does and turn "blocked on an instrument" into
    observable progress. Worst quiet stretch across the same six trials: 1.12s.

    A process wedged in a *single* uninterruptible syscall moves neither
    counter, which is exactly the case the watchdog exists to catch. This
    widens what counts as progress; it does not make a stuck process look busy.

    /proc/<pid>/schedstat was measured as an alternative and resolves no better
    -- both are limited by the same underlying transaction boundaries -- so
    this uses status, which needs no CONFIG_SCHEDSTATS.
    """
    try:
        with open(f'/proc/{pid}/status', 'rb') as f:
            data = f.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return 0
    total = 0
    for line in data.splitlines():
        if line.startswith((b'voluntary_ctxt_switches:',
                            b'nonvoluntary_ctxt_switches:')):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    total += int(parts[1])
                except ValueError:
                    pass
    return total


def progress_snapshot(pid):
    """
    A value that changes while ``pid``'s process tree is doing anything.

    CPU time and context switches across the whole tree, plus the tree's
    membership. Any script doing real work — computing, talking to an
    instrument, waiting on a USB transaction, spawning a helper — moves one of
    the three. A script wedged on a device that stopped answering moves none.

    Both counters are needed and neither is redundant. CPU time catches a
    teardown that is computing; context switches catch one that is *blocked*,
    which is what talking to hardware mostly is and which CPU time is nearly
    blind to (see :func:`ctxt_switches`). Tree membership catches a teardown
    whose work happens entirely in short-lived children.

    This is deliberately not tied to the lager net API. Hardware calls happen
    in-process inside the user's script (Acroname and friends coordinate
    through file locks), so the service never sees them, and a user driving
    their own instruments through their own library would be invisible to any
    API-level heartbeat. These counters cost nothing to emit and work for any
    cleanup code at all.

    Returns None where /proc is unavailable (macOS, for the unit tests), which
    callers treat as "no progress information" and fall back to a fixed wait.
    """
    if not os.path.isdir('/proc'):
        return None
    tree = process_tree(pid)
    if not tree:
        return None
    return (
        sum(cpu_ticks(p) for p in tree),
        sum(ctxt_switches(p) for p in tree),
        frozenset(tree),
    )


def begin(pids):
    """Record that ``pids`` are being reaped and the bench is not yet safe."""
    now = time.monotonic()
    with _lock:
        for pid in pids:
            _reaping.setdefault(pid, now)


def finish(pids):
    """Drop ``pids`` from the registry; their reap is over."""
    with _lock:
        for pid in pids:
            _reaping.pop(pid, None)


class reaping_job:
    """Context manager wrapping a reap in :func:`begin` / :func:`finish`."""

    def __init__(self, pids):
        self._pids = [pid for pid in pids if pid is not None]

    def __enter__(self):
        begin(self._pids)
        return self

    def __exit__(self, exc_type, exc, tb):
        finish(self._pids)
        return False


def reaping():
    """
    PIDs still shutting down, newest registration order not guaranteed.

    Prunes entries that have exited (the normal way an entry leaves, since a
    reaper that dies mid-escalation never gets to call :func:`finish`) and
    entries past :data:`STUCK_AFTER_S`.
    """
    now = time.monotonic()
    live = []
    with _lock:
        for pid, started in list(_reaping.items()):
            if not pid_is_alive(pid):
                del _reaping[pid]
                continue
            if now - started > STUCK_AFTER_S:
                logger.warning(
                    'pid %s has been shutting down for %.0fs and survived '
                    'SIGKILL; it is wedged, not cleaning up. Releasing the '
                    'bench anyway - it may be in an unsafe state.',
                    pid, now - started,
                )
                del _reaping[pid]
                continue
            live.append(pid)
    return live


def wait_until_clear(timeout, poll=POLL_INTERVAL_S):
    """
    Block until no job is shutting down.

    Args:
        timeout: seconds to wait before giving up.
        poll: seconds between checks.

    Returns:
        (bool, list[int]): whether the bench quiesced, and any PIDs still
        shutting down when we stopped waiting.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    started_waiting = None
    waited_on = ()
    while True:
        pending = reaping()
        if not pending:
            if started_waiting is not None:
                logger.info(
                    'bench quiesced after %.1fs (was waiting on pid(s) %s)',
                    time.monotonic() - started_waiting,
                    ', '.join(str(p) for p in waited_on),
                )
            return True, []
        if started_waiting is None:
            started_waiting = time.monotonic()
            waited_on = tuple(pending)
            logger.info(
                'a previous job is still shutting down (pid(s) %s); waiting '
                'up to %.0fs before starting the next one',
                ', '.join(str(p) for p in pending), timeout,
            )
        if time.monotonic() >= deadline:
            return False, pending
        time.sleep(poll)
