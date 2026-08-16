# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import atexit
import logging
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager

from lager.util.device_lock import DeviceLockError, device_lock
from lager.util.device_lock import default_manager as _default_lock_manager
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


# ─────────────  Bounded hub-session reuse  ─────────────
#
# Opening a hub is the expensive part of an operation — a BrainStem connect is
# native code that costs whole seconds — and interactive callers (dashboard and
# CLI users clicking enable/disable) arrive in bursts against the same hub. A
# session keeps the connection AND the cross-process flock warm for a short
# idle window after an operation, so the next operation in the burst reuses the
# open handle instead of paying a fresh connect.
#
# The window is the entire cost to everyone else: another process's worst-case
# wait for the hub is the idle window plus one operation. It is sized to cover
# the gap between a person's consecutive clicks (sub-second on a warm path)
# while staying comparable to what one open/operate/close cycle costs anyway —
# never an indefinite pin, which was the original cross-process contention bug.
HUB_SESSION_IDLE_S = 2.5

# How long the idle-expiry timer waits for the per-hub thread lock before
# giving up. If the lock is busy, an operation is mid-claim on this hub and
# ownership of the session (re-arm or teardown) has passed to it.
_EXPIRE_LOCK_WAIT_S = 0.1

# How long process exit will wait, in total, for expiry teardowns that are
# already running. Sized against a MEASURED healthy disconnect: a BrainStem
# hub takes on the order of two seconds to disconnect, so a bound any tighter
# would routinely abandon a teardown that was about to succeed. It stays far
# below the driver's own HUB_OP_TIMEOUT_S because a disconnect slower than
# this is a wedged hub, and waiting one out at exit buys nothing — the handle
# is lost either way, and the process is trying to leave.
_EXIT_TEARDOWN_WAIT_S = 10.0


class _HubSession:
    """One parked hub connection: the open handle, how to close it, and when
    the idle window ends."""

    __slots__ = ("handle", "close_fn", "expires_at", "timer")

    def __init__(self, handle, close_fn, expires_at, timer):
        self.handle = handle
        self.close_fn = close_fn
        self.expires_at = expires_at
        self.timer = timer


class HubSessionPool:
    """Claim/release primitives for per-hub exclusive access with bounded
    handle reuse.

    ``claim(key)`` takes the per-hub thread lock and ensures the cross-process
    flock is held, handing back any parked session handle. The caller runs its
    operation (under its own ``run_hub_op`` deadline) and finishes the claim
    with exactly one of:

    * ``hold()``   — success; park the handle and keep the flock for the idle
                     window (or the window's remainder, when the operation must
                     not extend it — the polling sweep).
    * ``close()``  — close the handle (if any), release flock and thread lock.
    * ``abandon_wedged()`` — a deadline expired with a thread still stuck
                     inside the driver. Nothing is closed (the stuck thread
                     owns the handle), the flock is released so OTHER processes
                     can try the hub, and the thread lock stays held so callers
                     in THIS process fail fast instead of wedging too — the
                     same poisoning ``hub_access`` performs.

    The flock is acquired and released through the lock manager directly, not
    the ``device_lock`` context manager: the manager is re-entrant per process
    but not refcounted, so a nested acquire/release inside a held session would
    silently drop the session's hold.

    A pool that parks anything registers an ``atexit`` drain for itself, so a
    process that exits inside an idle window still closes its handles; see
    ``_ensure_exit_drain``.

    ``now`` and ``timer_factory`` exist so tests can drive the idle window with
    a fake clock and hand-fired timers; production uses the real ones.
    """

    def __init__(self, *, idle_s=HUB_SESSION_IDLE_S, lock_manager=None,
                 now=time.monotonic, timer_factory=threading.Timer):
        self.idle_s = idle_s
        self._lock_manager = lock_manager or _default_lock_manager
        self._now = now
        self._timer_factory = timer_factory
        self._sessions: dict = {}
        # Guards the _sessions dict itself. Per-hub ordering is provided by
        # the per-hub thread lock; this only keeps dict mutation coherent
        # between a claimer and an expiry timer that lost the race.
        self._guard = threading.Lock()
        # Per pool rather than module-level, so a test that installs its own
        # pool starts from a clean slate. See _ensure_exit_drain.
        self._exit_drain_registered = False
        # key -> the timer thread currently tearing that session down, so exit
        # can wait for a disconnect it is too late to start itself. Guarded by
        # _guard. See _await_expiries.
        self._expiring: dict = {}

    # -- internal ------------------------------------------------------ #

    def _pop_session(self, key):
        with self._guard:
            session = self._sessions.pop(key, None)
        if session is not None and session.timer is not None:
            session.timer.cancel()
        return session

    def _park(self, key, handle, close_fn, expires_at):
        # Before the dict insert, and outside self._guard: a parked handle
        # must never be reachable without an exit drain registered for it,
        # and atexit.register allocates — allocation can trigger a GC, and
        # DeviceLockManager.__del__ is a real finalizer, which is not
        # something to run inside a lock the expiry timer also takes.
        self._ensure_exit_drain()
        remaining = expires_at - self._now()
        timer = self._timer_factory(remaining, lambda: self._expire(key))
        session = _HubSession(handle, close_fn, expires_at, timer)
        with self._guard:
            self._sessions[key] = session
        try:
            timer.daemon = True
        except AttributeError:
            pass
        timer.start()

    def _expire(self, key):
        """Idle window over: disconnect the parked handle and release the
        flock. Runs on the timer thread."""
        lock = _local_hub_lock(key)
        if not lock.acquire(timeout=_EXPIRE_LOCK_WAIT_S):
            # An operation is claiming this hub right now; it popped (or will
            # pop) the session and owns its lifecycle from here.
            return
        wedged = False
        try:
            with self._guard:
                session = self._sessions.get(key)
                if session is None or session.expires_at > self._now():
                    return  # claimed meanwhile, or re-armed with a new window
                self._sessions.pop(key, None)
            # The disconnect is a native call and gets the same deadline and
            # hang response as any other hub operation: a disconnect that
            # never returns is a wedged hub, and the hang hook (self-restart)
            # is the recovery for it.
            #
            # (Should this timer ever fire inside the interpreter's shutdown
            # window, run_hub_op cannot create its worker thread and raises
            # instead — the handler below swallows it and the handle leaks.
            # The window is sub-millisecond and _drain_at_exit has normally
            # popped the session already, so this stays a note rather than a
            # special case. It is also why the exit path closes inline; see
            # drain().)
            #
            # Published for the whole teardown, so an exiting main thread can
            # wait for a disconnect that started before it (see
            # _await_expiries). This thread blocks inside run_hub_op until the
            # close finishes, so joining it is joining the close.
            me = threading.current_thread()
            with self._guard:
                self._expiring[key] = me
            try:
                try:
                    run_hub_op(key, lambda: session.close_fn(session.handle))
                except HubOperationTimeout:
                    wedged = True  # hook already fired inside run_hub_op
                except Exception:
                    logger.exception("hub session close failed for %s", key)
                finally:
                    self._lock_manager.release_lock(key)
                logger.debug("hub session %s: idle window over, released", key)
            finally:
                with self._guard:
                    if self._expiring.get(key) is me:
                        del self._expiring[key]
        finally:
            if not wedged:
                lock.release()

    # -- exit cleanup -------------------------------------------------- #
    #
    # A parked session is an open vendor handle held past the end of the
    # operation that opened it, and the expiry timer that would close it is a
    # daemon thread (see _park) — deliberately, so a parked hub can never be
    # what keeps a `lager python` script's interpreter alive. The cost of that
    # choice is a script that finishes inside the idle window: it exits with
    # the handle still open, so BrainStem's ZeroMQ sockets are live when
    # CPython finalises, and czmq reports them dangling and aborts the
    # process. A test that passed dies with SIGABRT after printing its
    # results.
    #
    # atexit is the other half of the daemon-timer decision: do not hold the
    # interpreter open, but do close before it goes. Note that this is the
    # ONLY exit path where the problem exists — os._exit (self_restart),
    # SIGKILL and the box's timeout wrapper all skip Py_Finalize, so czmq
    # never runs its shutdown and the kernel reclaims the fd and the flock.

    def _drain_at_exit(self):
        """``atexit`` hook: close every parked handle while the interpreter
        is still whole.

        Never raises. This runs after a script's own logic has finished, so
        anything escaping here turns a passing test into a failing one — the
        exact outcome the handler exists to prevent. Same shape as
        ``io/labjack_handle._cleanup_on_exit``.
        """
        try:
            self.drain()
            self._await_expiries(_EXIT_TEARDOWN_WAIT_S)
        except BaseException:  # noqa: BLE001 — see docstring
            pass

    def _await_expiries(self, timeout):
        """Wait, bounded, for expiry teardowns already in flight.

        Draining the parked sessions is only half of it. Once the idle timer
        has fired, the session is gone from ``_sessions`` — so the drain finds
        nothing to do — while the disconnect it started is still running on a
        daemon thread. A disconnect costs on the order of seconds, so a script
        that exits in that gap has finalisation kill the teardown midway and
        leaves exactly the dangling sockets the drain exists to prevent. On a
        2.5s idle window and a ~2s disconnect that gap is roughly as wide as
        the window itself, and any test that idles a few seconds after its
        last hub operation lands in it.

        Bounded on the total, not per thread: the point is to outlast a
        healthy disconnect, never to wait out a wedged one.
        """
        end = time.monotonic() + timeout
        while True:
            with self._guard:
                pending = [t for t in self._expiring.values() if t.is_alive()]
            if not pending:
                return
            remaining = end - time.monotonic()
            if remaining <= 0:
                logger.debug(
                    "hub teardown still running after %.0fs at exit; leaving it",
                    timeout,
                )
                return
            pending[0].join(remaining)

    def _ensure_exit_drain(self):
        """Register ``_drain_at_exit`` the first time this pool parks anything.

        On first park rather than at import, for two reasons:

        * ``atexit`` runs LIFO, and the vendor SDK is imported lazily on the
          first open (``acroname._require_library``). Registering here lands
          after any vendor import the open path triggered, so a teardown that
          import registered runs after this one rather than before it. That is
          ordering insurance, not a guarantee — a vendor module imported later
          still registers later.
        * A process that never touches a hub pays nothing.

        The BOUND METHOD is registered on purpose: ``atexit`` then holds a
        strong reference to the pool. Otherwise the only thing keeping a pool
        with a parked session reachable is the ``self`` captured by the expiry
        timer's callback — incidental, and silently undone by anyone who makes
        that callback a weak reference.
        """
        if self._exit_drain_registered:
            return
        self._exit_drain_registered = True
        atexit.register(self._drain_at_exit)

    # -- public -------------------------------------------------------- #

    def claim(self, key, *, timeout):
        """Exclusive access to one hub, reusing a parked session if present.

        Raises ``DeviceLockError`` when either lock layer cannot be taken
        within ``timeout`` — the same contract as ``hub_access``.
        """
        lock = _local_hub_lock(key)
        if not lock.acquire(timeout=timeout):
            raise DeviceLockError(
                f"USB hub {key} is busy: another operation in this process "
                f"has held it for more than {timeout:.0f}s"
            )
        try:
            session = self._pop_session(key)
            if session is None:
                # No session, so this process holds no flock for the key;
                # take one (bounded, same timeout as the thread lock).
                self._lock_manager.acquire_lock(key, timeout=timeout)
                return _HubClaim(self, key, lock)
            return _HubClaim(self, key, lock, handle=session.handle,
                             close_fn=session.close_fn,
                             prior_expiry=session.expires_at)
        except BaseException:
            lock.release()
            raise

    def drain(self):
        """Synchronously tear down every parked session (tests, shutdown).

        Deliberately does NOT take the per-hub thread lock. It cannot need it:
        a session a live thread owns was already popped by ``claim``, so
        everything still in ``_sessions`` is a handle nobody is inside. And
        taking it would be actively wrong — after ``abandon_wedged`` that lock
        is held forever by design, so a drain that waited on it would hang on
        exactly the hub that poisoned the process. For the same reason a
        wedged handle is never closed here: it was never parked.

        The close runs INLINE, not through ``run_hub_op``. On the exit path a
        worker thread cannot be created at all ("can't create new thread at
        interpreter shutdown", CPython 3.12+), so the deadline would raise
        rather than bound anything and the handle would leak; and a
        ``run_hub_op`` timeout fires ``_hang_hook``, which for a box service
        ends in ``os._exit(70)`` — a clean exit turned into a failure by its
        own cleanup. A parked handle just completed an operation inside its
        deadline, so it is the one handle in the process known to be live.
        """
        with self._guard:
            keys = list(self._sessions)
        for key in keys:
            session = self._pop_session(key)
            if session is None:
                continue
            try:
                session.close_fn(session.handle)
            except Exception:
                logger.exception("hub session close failed for %s", key)
            finally:
                # Released per key, and even when the close blew up: one
                # unresponsive hub must not leave every other hub on the box
                # claimed for the next process.
                try:
                    self._lock_manager.release_lock(key)
                except Exception:
                    logger.exception("hub flock release failed for %s", key)


class _HubClaim:
    """One caller's tenure on a hub: locks held, plus the handle in play."""

    def __init__(self, pool, key, lock, handle=None, close_fn=None,
                 prior_expiry=None):
        self._pool = pool
        self._key = key
        self._lock = lock
        self.handle = handle
        self._close_fn = close_fn
        self._prior_expiry = prior_expiry
        # True when this claim started from a parked session — even if the
        # stale handle is later discarded and reopened.
        self.reused = handle is not None
        self._finished = False

    def adopt(self, handle, close_fn):
        """Record a freshly opened handle (and how to close it later)."""
        self.handle = handle
        self._close_fn = close_fn

    def discard(self):
        """Drop the current handle (best-effort close), keeping both locks.
        Used when a reused handle turns out stale mid-operation."""
        handle, close_fn = self.handle, self._close_fn
        self.handle = None
        if handle is not None and close_fn is not None:
            try:
                close_fn(handle)
            except Exception:
                logger.exception("hub session close failed for %s", self._key)

    def hold(self, *, refresh):
        """Success: park the handle for the idle window and release only the
        thread lock. ``refresh=False`` keeps the PRIOR window's expiry — the
        polling sweep may ride an existing session but must never be what
        keeps one alive — and closes instead when that expiry has passed.

        Returns True when the handle was actually parked, False when it fell
        through to ``close()`` (no handle, or a window with no time left)."""
        if self._finished:
            return False
        pool = self._pool
        if refresh or self._prior_expiry is None:
            expires_at = pool._now() + pool.idle_s
        else:
            expires_at = self._prior_expiry
        if self.handle is None or expires_at <= pool._now():
            self.close()
            return False
        self._finished = True
        pool._park(self._key, self.handle, self._close_fn, expires_at)
        self._lock.release()
        return True

    def close(self):
        """Close the handle (if any) and release both locks."""
        if self._finished:
            return
        self._finished = True
        self.discard()
        self._pool._lock_manager.release_lock(self._key)
        self._lock.release()

    def abandon_wedged(self):
        """A deadline expired with a thread stuck inside the driver. Release
        the flock (the wedge is per-process; another process may still open
        the hub) but NOT the thread lock, so later callers in this process
        fail fast rather than queueing into the same wedge."""
        if self._finished:
            return
        self._finished = True
        self.handle = None  # owned by the stuck thread now; never close it
        self._pool._lock_manager.release_lock(self._key)


class USBNet(ABC):
    """Abstract base class for USB network controllers."""

    # Does this driver keep a USB handle alive between public calls?
    #
    # Only a driver that does can have its handle orphaned by a re-enumeration,
    # which is the sole failure mode the box's self-restart recovery repairs
    # (see util/self_restart.py). A driver that opens and closes inside every
    # call has nothing to orphan, so restarting the service on its behalf drops
    # every other in-flight box operation to fix nothing.
    #
    # Defaults True so an unmodified driver keeps today's behaviour; a driver
    # opts out only by demonstrating the open/close-per-call invariant.
    holds_usb_context_between_ops = True

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

    def states(self, ports, *, timeout=None):
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
            timeout: optional bound, in seconds, on the WHOLE read -- lock
                wait plus session -- so a shared caller budget (the state
                sweep's, issue #205) survives one slow hub. None keeps each
                driver's own ``HUB_OP_TIMEOUT_S``. This fallback ignores it:
                per-port ``state()`` calls already carry the driver deadline,
                and a fallback that partially honours a budget would read as
                the driver honouring it.

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


# ─────────────  Why a hub would not open  ─────────────
#
# "The hub did not open" has several causes with DIFFERENT remedies, and
# collapsing them is what makes a bench look intermittently broken: a hub that
# is unplugged and a hub that is cabled but not answering produce the same
# `state: null`, and only one of them is worth walking over to the bench for.
#
# Plain strings, not an enum: these cross an HTTP boundary into JSON and land in
# a CLI that may be a different version, where an unknown string is still
# printable. An enum would need `.value` at every boundary and buy nothing.
HUB_ABSENT = "hub-absent"                    # nothing from this vendor on the bus
HUB_UNREACHABLE = "hub-unreachable"          # our serial IS on the bus, will not answer
HUB_SERIAL_MISMATCH = "hub-serial-mismatch"  # vendor devices present, none ours
HUB_OPEN_FAILED = "hub-open-failed"          # sysfs unknown / other refusal
HUB_SKIPPED = "hub-skipped"                  # not probed: slower instruments consumed the state budget


class DeviceNotFoundError(USBBackendError):
    """Requested hub (by serial) could not be opened.

    Carries structured fields alongside the message so callers can act on the
    cause without parsing prose:

    ``classification``
        One of the ``HUB_*`` constants above, or None from a driver that does
        not classify.
    ``usb_context_healthy``
        True when this process demonstrably talked to the USB bus while failing
        to find THIS hub — i.e. a discovery scan completed and returned at least
        one device. That is positive evidence the process's USB context is fine
        and the hub is the problem, which is the one thing that distinguishes
        "restarting this service would help" from "restarting it is pointless".
        None when unknown; never guess.
    ``detail``
        The full open-attempt breakdown, for ``--json`` and diagnostics. Kept
        off the message so a remedy sentence is not buried in return codes.

    All three default to None, so ``DeviceNotFoundError(msg)`` still works.
    ``super().__init__(message)`` keeps ``.args`` single-element, so pickling
    and copying across the pyvisa/joulescope paths are unaffected.
    """

    def __init__(self, message, *, classification=None,
                 usb_context_healthy=None, detail=None):
        super().__init__(message)
        self.classification = classification
        self.usb_context_healthy = usb_context_healthy
        self.detail = detail


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
