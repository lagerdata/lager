# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Keep one host agent's processes from outliving it.

The supervisor (``python -m lager.host run``) stops its children when it is
asked to stop. When it is killed outright -- ``launchctl kickstart -k``, a
crash, the out-of-memory killer -- nothing asks, and a child left behind
keeps its port. The next agent's worker then fails to bind and restarts
every two seconds for as long as the leftover lives; seen on the bench Mac,
where one survived two reinstalls and kept serving hardware from a deleted
source tree.

Two independent guards, both native-host only (a Linux Box does not run
this package):

* each Python child watches its parent and exits when it changes;
* a starting supervisor ends any agent child that has no supervisor.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

PARENT_POLL_S = 1.0
EXIT_GRACE_S = 5.0
STALE_TERM_WAIT_S = 5.0
# The Python children `run` starts, as they appear in a process listing: a
# Python interpreter, by absolute path, whose first arguments are exactly
# these. The path may contain spaces ("Application Support").
CHILD_COMMAND = re.compile(r"^/.*[Pp]ython[\d.]*\s+-m\s+lager\.host\s+(worker|debug)(\s|$)")


def watch_parent(poll_s: float = PARENT_POLL_S, grace_s: float = EXIT_GRACE_S,
                 getppid=os.getppid, sleep=time.sleep, terminate=None, force=os._exit,
                 daemon: bool = True) -> threading.Thread:
    """Exit this process once the process that started it is gone.

    Asks politely first (SIGTERM to itself, so the normal cleanup runs) and
    leaves regardless after ``grace_s``.
    """
    parent = getppid()
    terminate = terminate or (lambda: os.kill(os.getpid(), signal.SIGTERM))

    def watch():
        while getppid() == parent:
            sleep(poll_s)
        logger.warning("the host agent that started this process (pid %s) is gone; exiting",
                       parent)
        terminate()
        sleep(grace_s)
        force(0)

    thread = threading.Thread(target=watch, name="parent-watch", daemon=daemon)
    thread.start()
    return thread


def _listing(run=subprocess.run) -> str:
    result = run(["ps", "-axwwo", "pid=,ppid=,uid=,command="],
                 capture_output=True, text=True, timeout=10.0, check=False)
    return result.stdout or ""


def find_stale(extra_commands=(), listing=None, uid=None, own_pid=None) -> list:
    """Pids of agent children whose supervisor is gone (parent is launchd).

    ``extra_commands`` are full paths of non-Python children (the
    oscilloscope daemon), matched as the start of the command line.
    """
    uid = os.getuid() if uid is None else uid
    own_pid = os.getpid() if own_pid is None else own_pid
    extra = tuple(c for c in extra_commands if c)
    stale = []
    for line in (_listing() if listing is None else listing).splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        pid, ppid, owner, command = int(parts[0]), parts[1], parts[2], parts[3]
        if ppid != "1" or owner != str(uid) or pid == own_pid:
            continue
        if CHILD_COMMAND.match(command) \
                or any(command == c or command.startswith(c + " ") for c in extra):
            stale.append(pid)
    return stale


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap_stale(extra_commands=(), find=find_stale, kill=os.kill, alive=_alive,
               sleep=time.sleep, wait_s: float = STALE_TERM_WAIT_S) -> list:
    """End leftover agent children before new ones are started. The pids."""
    try:
        stale = find(extra_commands)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not look for leftover agent processes: %s", exc)
        return []
    for pid in stale:
        logger.warning("ending leftover agent process %s from an earlier agent", pid)
        try:
            kill(pid, signal.SIGTERM)
        except OSError:
            pass
    waited = 0.0
    while waited < wait_s and any(alive(pid) for pid in stale):
        sleep(0.2)
        waited += 0.2
    for pid in stale:
        if alive(pid):
            logger.warning("leftover agent process %s ignored the request; killing it", pid)
            try:
                kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return stale
