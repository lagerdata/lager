# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""One J-Link operation per probe at a time, across threads and processes.

J-Link lets several applications open one probe at once. It does not stop them
from interleaving: while one JLinkExe downloads its flash RAMCode into target
RAM, a second client that halts, resets or reads the target can corrupt that
download, and J-Link then reports ``Verification of RAMCode failed`` /
``Failed to download RAMCode!`` with nothing programmed.

The box had nothing to stop that. The debug service is a threaded HTTP server,
so a ``/debug/flash`` and a ``/debug/connect`` or ``/debug/memrd`` for the same
probe ran side by side, and a ``lager python`` script driving the Net API ran
beside both. ``probe_lock(serial, operation)`` serialises them: the second
caller waits for the first to finish, and gives up with ``ProbeBusyError``
after ``LAGER_PROBE_LOCK_TIMEOUT_S`` seconds (default 300, longer than any
flash we have measured).

Two layers, because each covers what the other cannot:

* a per-probe ``threading.RLock`` orders threads of one process and makes the
  lock re-entrant, so ``flash_device()`` can hold it across its whole sequence
  while the helpers it calls take it again;
* an ``flock`` on ``/tmp/lager-probe-locks/<serial>.lock`` orders processes.
  The kernel drops it when the holder exits, so a killed process never leaves
  the probe locked.

Standard library only: ``gdbserver.py`` and ``api.py`` import it.
"""

import fcntl
import functools
import inspect
import logging
import os
import re
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

LOCK_DIR = '/tmp/lager-probe-locks'
DEFAULT_TIMEOUT_S = 300.0
_POLL_S = 0.1


class ProbeBusyError(Exception):
    """Another operation held the probe for longer than the wait allows."""


def _timeout_s():
    raw = os.environ.get('LAGER_PROBE_LOCK_TIMEOUT_S', '').strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value >= 0 else DEFAULT_TIMEOUT_S


def _key(serial):
    """Lock-file stem for a probe. ``None`` is the legacy single-probe path."""
    if not serial:
        return 'default'
    return re.sub(r'[^A-Za-z0-9_-]', '_', str(serial))


class _ProbeLock:
    def __init__(self, key):
        self.key = key
        self.rlock = threading.RLock()
        self.depth = 0
        self.fd = None
        self.holder = None

    @property
    def path(self):
        return os.path.join(LOCK_DIR, f'{self.key}.lock')

    def _other_holder(self):
        """What another process wrote into the lock file, for the error."""
        try:
            with open(self.path, encoding='utf-8') as f:
                return f.read().strip() or None
        except OSError:
            return None

    def acquire(self, operation, timeout):
        deadline = time.monotonic() + timeout
        if not self.rlock.acquire(blocking=False):
            # Another thread of this process holds it. Said in the log so the
            # service log shows requests being ordered, not only processes.
            logger.info('Probe %s busy (%s); %s waiting for it',
                        self.key, self.holder or 'another thread', operation)
            if not self.rlock.acquire(timeout=timeout):
                raise ProbeBusyError(self._busy_message(operation, timeout, self.holder))
        if self.depth:
            self.depth += 1
            return
        try:
            os.makedirs(LOCK_DIR, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
            waited = False
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        holder = self._other_holder()
                        os.close(fd)
                        raise ProbeBusyError(
                            self._busy_message(operation, timeout, holder))
                    if not waited:
                        logger.info('Probe %s busy (%s); %s waiting for it',
                                    self.key, self._other_holder() or 'another process',
                                    operation)
                        waited = True
                    time.sleep(_POLL_S)
        except BaseException:
            self.rlock.release()
            raise
        self.fd = fd
        self.depth = 1
        self.holder = f'{operation} (pid {os.getpid()})'
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f'{self.holder}\n'.encode())
        except OSError:
            pass  # the note is for error messages only

    def release(self):
        self.depth -= 1
        if self.depth == 0:
            fd, self.fd, self.holder = self.fd, None, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self.rlock.release()

    def _busy_message(self, operation, timeout, holder):
        return (f'Debug probe {self.key} is busy'
                f'{f" with {holder}" if holder else ""}; {operation} gave up after '
                f'{timeout:g} s. Wait for the other operation to finish and retry.')


_registry_lock = threading.Lock()
_registry = {}


def _lock_for(serial):
    key = _key(serial)
    with _registry_lock:
        lock = _registry.get(key)
        if lock is None:
            lock = _registry[key] = _ProbeLock(key)
        return lock


@contextmanager
def probe_lock(serial, operation):
    """Hold probe *serial* exclusively for *operation* (a short label)."""
    lock = _lock_for(serial)
    lock.acquire(operation, _timeout_s())
    try:
        yield
    finally:
        lock.release()


def holds_probe(operation):
    """Decorate a function taking ``serial`` so it runs under ``probe_lock``.

    A generator function holds the lock while it is iterated -- from the first
    ``next()`` until it finishes or is closed -- and keeps its return value.
    """
    def decorate(fn):
        signature = inspect.signature(fn)

        def serial_of(args, kwargs):
            bound = signature.bind_partial(*args, **kwargs)
            return bound.arguments.get('serial')

        if inspect.isgeneratorfunction(fn):
            @functools.wraps(fn)
            def gen_wrapper(*args, **kwargs):
                with probe_lock(serial_of(args, kwargs), operation):
                    return (yield from fn(*args, **kwargs))
            return gen_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with probe_lock(serial_of(args, kwargs), operation):
                return fn(*args, **kwargs)
        return wrapper
    return decorate
