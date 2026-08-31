# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Cross-process advisory locks for USB instruments.

Multiple box-side processes can race for the same USB instrument when the
shared hardware_service session pool isn't the only opener — e.g. an ad-hoc
`docker exec python3 -c "import pyvisa; ..."` debug script, a TUI launched
directly on the box, or an MCP tool invoked outside the normal /invoke path.
Two libusb opens against the same USB-TMC interface race on `set_configuration`
and the loser sees `[Errno 16] Resource busy`.

This module provides a small `fcntl.flock`-based lock keyed on VISA address.
Acquire the lock around any pyvisa `open_resource()` call to a USB-TMC
instrument; the lock is released after the open completes. Holding the lock
only across the open (not the full device lifetime) is intentional —
hardware_service already serializes subsequent SCPI calls via its in-process
per-address lock (see hardware_service._get_address_lock).

Originally extracted from `lager.power.solar.ea.DeviceLockManager` in 0.20.0
so the same pattern can defend Keithley / Rigol / Keysight USB-TMC drivers,
which previously had no protection against direct-instantiation racing.
"""

from __future__ import annotations

import os
import re
import time
import fcntl
import logging
import tempfile
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class DeviceLockError(Exception):
    """Raised when a device lock cannot be acquired within the timeout."""
    pass


def _pid_is_alive(pid: int) -> bool:
    """True if `pid` names a live process.

    Signal 0 performs the existence and permission checks without delivering
    anything. EPERM means the process exists but belongs to another user, which
    still counts as alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Can't tell. Assume alive: wrongly stealing a live holder's lock puts
        # two processes on one USB instrument, which is worse than refusing.
        return True
    return True


class DeviceLockManager:
    """fcntl-based cross-process lock keyed on VISA address.

    Lock files live under `${TMPDIR}/<lock_subdir>/`. Acquiring is non-blocking
    with a polling fallback (50 ms sleep) until `timeout` elapses, at which
    point `DeviceLockError` is raised.

    If the locking mechanism itself fails (file create/open error, permission
    denied, etc.) we log and *fail open* — return True so the operation isn't
    blocked. The lock is advisory: blocking on locking-infrastructure failures
    would convert a transient FS issue into a hard outage. This matches the
    EA driver's longstanding behavior.
    """

    def __init__(self, lock_subdir: str = 'lager_device_locks'):
        self.lock_dir = os.path.join(tempfile.gettempdir(), lock_subdir)
        os.makedirs(self.lock_dir, exist_ok=True)
        self.lock_handles: dict[str, object] = {}

    def _get_lock_path(self, address: str) -> str:
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', address)
        return os.path.join(self.lock_dir, f'device_{safe_name}.lock')

    @staticmethod
    def _read_holder_pid(lock_file) -> int | None:
        """Read the PID the current holder recorded, or None if unreadable.

        The file is written by `_record_pid` after a successful flock and is
        never truncated on the failure path, so under contention it still
        holds the winner's PID.
        """
        try:
            lock_file.seek(0)
            raw = lock_file.read(32).strip()
        except Exception:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def acquire_lock(self, address: str, timeout: float = 2.0) -> bool:
        """Acquire an exclusive advisory lock on `address`.

        Returns True on success. Raises `DeviceLockError` if the timeout
        elapses with another process holding the lock. Returns True (fail-open)
        if the locking mechanism itself errors.
        """
        # Joined inline rather than through ``_get_lock_path``: a containment
        # check is only credited to the function that performs the join, and
        # this is the function that opens the result. See lager.util.paths.
        # The slug is the real defence.
        _safe = re.sub(r'[^a-zA-Z0-9_-]', '_', address)
        lock_path = os.path.normpath(
            os.path.join(self.lock_dir, f'device_{_safe}.lock'))
        if not lock_path.startswith(self.lock_dir + os.sep):
            raise DeviceLockError(
                f'refusing a lock path outside {self.lock_dir!r}')

        if address in self.lock_handles:
            return True  # already held by this process

        lock_file = None
        start = time.time()
        try:
            # Open without truncating — preserves the existing holder's PID
            # in the file until we successfully acquire the lock. Opening
            # with 'w' would erase that debugging info even if our own
            # acquire then times out, leaving the lock file empty under
            # contention.
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            lock_file = os.fdopen(fd, 'r+')

            def _record_pid() -> None:
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.write(f'{os.getpid()}\n')
                lock_file.flush()

            # Non-blocking attempt first — common case.
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.lock_handles[address] = lock_file
                _record_pid()
                return True
            except (IOError, OSError):
                pass

            # Poll with a small sleep until timeout.
            while (time.time() - start) < timeout:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.lock_handles[address] = lock_file
                    _record_pid()
                    return True
                except (IOError, OSError):
                    time.sleep(0.05)

            # Timed out. Read back the PID the holder recorded so the error can
            # name it. This is diagnostics, NOT a stale-lock steal.
            #
            # A steal would be dead code here. The kernel drops a flock when
            # the last fd on the open file description closes, so a holder that
            # simply died -- even by SIGKILL -- has already released the lock
            # and we would have taken it in the poll loop above. The only way
            # to reach this point with a dead recorded PID is that the holder
            # forked and exited while a descendant kept the inherited fd. The
            # lock is then genuinely still held by a live process, so there is
            # nothing safe to steal: forcing it would put two processes on one
            # USB instrument, which is the failure this lock exists to prevent.
            #
            # What is actually useful is saying which of those two situations
            # this is, because the remedy differs: kill the named process, or
            # hunt the orphaned descendant it left behind.
            holder_pid = self._read_holder_pid(lock_file)

            if lock_file:
                lock_file.close()

            if holder_pid is None:
                who = 'another process'
            elif _pid_is_alive(holder_pid):
                who = f'pid {holder_pid}, which is still running'
            else:
                who = (f'an orphaned child of pid {holder_pid} -- that process has '
                       f'exited but left the lock fd open in a descendant')
            raise DeviceLockError(
                f'Device at {address} is locked by {who} '
                f'(waited {timeout:.1f}s). Another lager-box process likely has '
                f'this USB instrument open. Inspect with: '
                f"sudo lsof | grep '{self.lock_dir}'"
            )

        except DeviceLockError:
            raise
        except Exception as e:
            # Locking infrastructure failed (FS, perms, etc.) — log and
            # fail-open so we don't break legitimate work.
            if lock_file is not None:
                try:
                    lock_file.close()
                except Exception:
                    pass
            logger.warning(f'Device locking failed for {address}: {e}')
            return True

    def release_lock(self, address: str) -> None:
        """Release the lock on `address`. Safe to call when not held."""
        lock_file = self.lock_handles.pop(address, None)
        if lock_file is None:
            return
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.warning(f'flock LOCK_UN failed for {address}: {e}')
        try:
            lock_file.close()
        except Exception as e:
            logger.warning(f'lock file close failed for {address}: {e}')

    def __del__(self):
        for address in list(self.lock_handles.keys()):
            try:
                self.release_lock(address)
            except Exception:
                pass


# Default manager for USB-TMC drivers (Keithley, Rigol, Keysight, etc.).
# EA drivers keep their own manager under `lager_ea_locks` to preserve their
# pre-existing on-disk lock directory.
default_manager = DeviceLockManager()


@contextmanager
def device_lock(address: str, timeout: float = 2.0, manager: DeviceLockManager | None = None):
    """Context manager wrapping acquire/release on the default (or provided)
    manager. Use around `pyvisa.ResourceManager().open_resource(address)`:

        with device_lock(address, timeout=2.0):
            inst = rm.open_resource(address)
        # lock released; subsequent SCPI calls serialize via hardware_service
        # per-address lock instead.
    """
    mgr = manager if manager is not None else default_manager
    mgr.acquire_lock(address, timeout=timeout)
    try:
        yield
    finally:
        mgr.release_lock(address)
