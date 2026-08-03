# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import logging
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager

from lager.util.device_lock import DeviceLockError, device_lock
from lager.util.watchdog import run_with_deadline

logger = logging.getLogger(__name__)

# ─────────────  Exclusive access to a physical USB hub  ─────────────
#
# A hub's USB/libusb interface is EXCLUSIVE, yet it is driven from several box
# processes (box_http_server's `lager usb` path, the MCP server, and each
# `lager python` test — its own subprocess) AND potentially several threads in
# one of them. Two layers are needed:
#   * cross-process — `device_lock` (fcntl.flock, shared /tmp). Its manager is
#     re-entrant WITHIN a process, so it alone does not serialise threads.
#   * in-process — a per-hub `threading.Lock`.
# `hub_access` combines both (the same belt-and-suspenders as hardware_service),
# keyed on the physical hub so different hubs never block each other.
#
# BOTH layers are bounded. The flock always was; the in-process lock was taken
# with a plain `with`, so a thread stuck in a wedged driver call held it forever
# and every later caller — including the 1 Hz state polls — queued behind it
# with no timeout of their own. The two now share one timeout, and a caller that
# cannot get in is told so instead of waiting.
_local_hub_locks: dict = {}
_local_hub_locks_guard = threading.Lock()

# How long a hub operation (the whole open → operate → close cycle, including a
# driver's internal retry) may take before the caller stops waiting on it. Well
# above any healthy operation — a cold BrainStem discovery scan can take
# several seconds — so expiry means "wedged", not "slow".
HUB_OP_TIMEOUT_S = 30.0


def _local_hub_lock(key: str) -> threading.Lock:
    with _local_hub_locks_guard:
        lock = _local_hub_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _local_hub_locks[key] = lock
        return lock


@contextmanager
def hub_access(key: str, timeout: float):
    """Exclusive access to one physical USB hub, within AND across processes.
    Wrap a driver's whole open→operate→release cycle in this.

    Raises ``DeviceLockError`` if the hub cannot be claimed within ``timeout``
    — the same type the cross-process ``device_lock`` raises, so callers need
    one handler for "hub unavailable" whichever layer refused.

    If the body raises ``HubOperationTimeout`` the in-process lock is
    deliberately NOT released: a thread is still inside the hub's driver, and
    handing the hub to the next thread would only wedge that one too. The
    cross-process flock IS released, because the wedge is per-process — a
    different process (a ``lager python`` script, or this service after the
    supervisor respawns it) can still open the hub.
    """
    lock = _local_hub_lock(key)
    if not lock.acquire(timeout=timeout):
        raise DeviceLockError(
            f"USB hub {key} is busy: another operation in this process has "
            f"held it for more than {timeout:.0f}s"
        )
    wedged = False
    try:
        with device_lock(key, timeout=timeout):
            try:
                yield
            except HubOperationTimeout:
                wedged = True
                raise
    finally:
        if not wedged:
            lock.release()


# Called with the hub's lock key when an operation blows its deadline. Left
# unset here on purpose: recovery from a hang means replacing the process, and
# only a long-lived service running under the supervisor may decide that. A
# `lager python` script imports these same drivers and must never exit itself.
# box_http_server installs its hook when it registers the USB routes.
_hang_hook = None


def set_hang_hook(hook):
    """Register the process's response to a hung hub operation (or None).

    Registered here rather than at each call site because a hang is a property
    of the PROCESS, not of the request that happened to notice it: the 1 Hz
    ``/nets/state`` poll is the likeliest first witness, and ``dispatcher.states``
    deliberately swallows one hub's failure so it cannot lose the others. With
    the response wired to the detector instead, every path — ``/usb/command``,
    a state sweep, an MCP tool — triggers recovery from the same place.
    """
    global _hang_hook  # pylint: disable=global-statement
    _hang_hook = hook


def run_hub_op(key: str, fn, timeout: float = HUB_OP_TIMEOUT_S):
    """Run one hub operation under a deadline, inside ``hub_access``.

    BrainStem's ``discoverAndConnect``/``connectFromSpec`` and pykush's HID
    calls are native code that can block indefinitely when a hub's USB link is
    wedged after a re-enumeration. Nothing below Python can interrupt them, so
    the deadline abandons the worker rather than cancelling it — what it buys
    is a caller that answers instead of joining the pile-up, and a signal the
    service can act on (the supervisor respawn).
    """
    try:
        return run_with_deadline(
            fn, timeout, what=f"USB hub operation on {key}",
            timeout_error=HubOperationTimeout,
        )
    except HubOperationTimeout:
        hook = _hang_hook
        if hook is not None:
            try:
                hook(key)
            except Exception:  # noqa: BLE001 — recovery must not mask the hang
                logger.exception("hub hang hook failed for %s", key)
        raise


class USBNet(ABC):
    """Abstract base class for USB network controllers."""
    @abstractmethod
    def enable(self, net_name, port):
        """Enable (power on) the specified port on the given USB net."""
        raise NotImplementedError()

    @abstractmethod
    def disable(self, net_name, port):
        """Disable (power off) the specified port on the given USB net."""
        raise NotImplementedError()

    @abstractmethod
    def toggle(self, net_name, port):
        """Toggle the power state of the specified port on the given USB net.

        Returns:
            bool: the resulting port state — True if the port is now enabled
            (powered on), False if it is now disabled (powered off).
        """
        raise NotImplementedError()

    @abstractmethod
    def state(self, net_name, port):
        """Read the current power state of the specified port without changing it.

        Returns:
            bool: True if the port is currently enabled (powered on), False if
            it is currently disabled (powered off).
        """
        raise NotImplementedError()

    def _lock_key(self) -> str:
        """Key identifying the *physical* hub this controller talks to.

        Already a de-facto interface member: each driver's session helper
        serialises on it via ``hub_access``, so two controllers sharing a key
        contend and two with different keys do not. Declared here so callers
        that need to group nets by hub (see ``dispatcher.states``) can rely on
        it rather than reaching into a driver private.
        """
        raise NotImplementedError()

    def states(self, ports):
        """Read several ports on THIS hub, ideally in one session.

        Every driver wraps each public call in its own
        open -> operate -> close cycle under ``hub_access``, because holding a
        hub open would pin its exclusive USB claim away from other processes.
        That is the right default for one-shot commands, but it makes reading N
        ports cost N full enumerate/connect/disconnect cycles, serialised behind
        this hub's lock -- which is what made a whole-bench state sweep take
        seconds per hub rather than milliseconds.

        Drivers should override this to run all the reads inside a single
        session. This base implementation is a correct-but-slow fallback so a
        driver that has not been taught the batch form still works.

        Args:
            ports: iterable of port numbers on this hub.

        Returns:
            dict[int, bool | None]: port -> enabled, or None for a port whose
            individual read failed. Never raises for a single bad port; a
            failure that takes out the whole hub still propagates.
        """
        out = {}
        for port in ports:
            try:
                out[port] = bool(self.state(None, port))
            except Exception:
                out[port] = None
        return out
    
    # ─────────────  Common backend exceptions  ─────────────

class USBBackendError(RuntimeError):
    """Base class for all lager.usb_hub backend failures."""


class LibraryMissingError(USBBackendError):
    """Required vendor SDK (BrainStem, pykush, …) is not present."""


class DeviceNotFoundError(USBBackendError):
    """Requested hub (by serial) could not be opened."""


class PortStateError(USBBackendError):
    """Hub reported an error while reading or changing port state."""


class HubOperationTimeout(USBBackendError):
    """A hub operation blocked past its deadline and was abandoned.

    Distinct from ``DeviceLockError``, which means another caller holds the hub
    and this one may retry. This means a thread is stuck *inside* the hub's
    driver, in native code Python cannot interrupt, and only a fresh process
    clears it. Handlers must match it BEFORE the generic ``USBBackendError``
    case, or a hang reports as an ordinary backend error.
    """
