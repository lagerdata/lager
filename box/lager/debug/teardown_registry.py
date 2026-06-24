# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Opt-in teardown of debug gdbservers connected by the current process.

A ``lager python`` job that calls ``DebugNet.connect()`` but is then aborted
(Ctrl-C / SIGTERM) or exits via an exception never reaches ``disconnect()``, so
the detached ``-stayrunning`` JLinkGDBServer keeps holding the probe and the
next connect fails ("Failed to connect to target device") until the USB hub
port is bounced.

When ``LAGER_DEBUG_AUTOTEARDOWN`` is set to an on-value (``1/true/yes/on``),
every debug net this process connects is recorded here, and an ``atexit`` hook
plus chaining SIGTERM/SIGINT handlers call ``disconnect()`` on each one when the
job ends (normal exit, exception, or abort).

Default OFF: intentionally long-lived servers that persist across separate
``lager python`` connect/flash jobs are unaffected unless the operator opts in.
"""

import atexit
import logging
import os
import signal
import threading

logger = logging.getLogger(__name__)

_ON_VALUES = {'1', 'true', 'yes', 'on'}

_lock = threading.RLock()
_registered = {}          # id(net) -> net
_handlers_installed = False
_prev_handlers = {}       # signum -> previous handler returned by getsignal()


def _enabled():
    """True only when ``LAGER_DEBUG_AUTOTEARDOWN`` is an explicit on-value."""
    return os.environ.get('LAGER_DEBUG_AUTOTEARDOWN', '').strip().lower() in _ON_VALUES


def register(net):
    """Record *net* for teardown at process exit. No-op when disabled.

    A strong reference is intentional: a fire-and-forget
    ``Net.get(...).connect()`` that keeps no handle must still be torn down on
    abort, so a ``WeakSet`` would drop exactly the nets we care about.
    """
    if not _enabled():
        return
    with _lock:
        _registered[id(net)] = net


def unregister(net):
    """Drop *net* (e.g. after an explicit ``disconnect``). Always safe to call."""
    with _lock:
        _registered.pop(id(net), None)


def teardown_all():
    """Disconnect every registered net. Never raises (teardown must not mask)."""
    with _lock:
        nets = list(_registered.values())
        _registered.clear()
    for net in nets:
        try:
            net.disconnect()
        except BaseException as exc:  # noqa: BLE001 — teardown must not raise
            logger.debug(
                'Auto-teardown disconnect failed for %r: %s',
                getattr(net, 'name', net), exc,
            )


def _signal_handler(signum, frame):
    teardown_all()
    # Chain to the previous handler so default abort semantics are preserved
    # (KeyboardInterrupt for SIGINT, process termination for SIGTERM).
    prev = _prev_handlers.get(signum)
    if callable(prev):
        prev(signum, frame)
        return
    # SIG_DFL / SIG_IGN / None: restore the prior disposition and re-raise so the
    # process ends exactly as it would have without our handler.
    try:
        signal.signal(signum, prev if prev is not None else signal.SIG_DFL)
    except (ValueError, OSError):
        pass
    os.kill(os.getpid(), signum)


def install_handlers():
    """Install ``atexit`` + SIGTERM/SIGINT teardown hooks once. No-op when disabled.

    Idempotent and conservative: chains (does not clobber) any handler already
    installed, and only acts when ``LAGER_DEBUG_AUTOTEARDOWN`` is enabled. If
    called off the main thread (where ``signal.signal`` is illegal), the
    ``atexit`` hook is still registered so normal exit is covered.
    """
    global _handlers_installed
    if not _enabled():
        return
    with _lock:
        if _handlers_installed:
            return
        _handlers_installed = True
        atexit.register(teardown_all)
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                _prev_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, _signal_handler)
            except (ValueError, OSError):
                # Not in the main thread, or signal unsupported on this platform.
                _prev_handlers.pop(signum, None)
