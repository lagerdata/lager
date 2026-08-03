# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Bound a blocking call that cannot be interrupted.

Instrument work on the box ends up inside native code — BrainStem's
``discoverAndConnect``, hidapi reads, libusb ``open``, vendor ``.so`` drivers.
When a device's USB link is wedged (typically right after a re-enumeration)
those calls can block forever: no exception, no timeout, no return. Everything
built on top of them inherits that: a lock the caller holds is never released,
the next request queues behind it, and the service is dead while still
answering its health check.

A Python thread cannot be killed, so this deadline does NOT cancel the work.
What it does is stop the *caller* waiting on it:

* the request answers with a real, structured error instead of hanging, and
* the caller can act on the hang — for box services that means scheduling the
  supervisor respawn (``util/self_restart.py``), which IS the recovery for an
  orphaned USB context.

The abandoned worker is a daemon thread, so it can never hold up interpreter
exit; it dies with the process when the supervisor restarts the service.
"""

from __future__ import annotations

import threading

DEFAULT_DEADLINE_S = 30.0


class OperationTimeout(Exception):
    """A bounded operation did not finish inside its deadline."""


def run_with_deadline(fn, timeout=DEFAULT_DEADLINE_S, *, what="operation",
                      timeout_error=OperationTimeout):
    """Run ``fn()`` on a worker thread, waiting at most ``timeout`` seconds.

    Returns ``fn``'s value, or re-raises whatever it raised. If the deadline
    expires first, raises ``timeout_error`` and leaves the worker running —
    see the module docstring for why abandoning it is the only option.

    ``timeout_error`` lets each subsystem raise a type its own callers already
    classify (e.g. a ``USBBackendError`` subclass for the hub drivers) rather
    than making every caller learn about this module.

    Re-raising the worker's exception preserves its traceback: ``raise exc``
    on an exception that already carries ``__traceback__`` appends the current
    frame rather than replacing the frames from the worker. Callers that
    classify failures by inspecting ``traceback.format_exc()`` —
    ``self_restart.looks_like_open_failure`` and friends — still see the
    driver frames that tell them what actually failed.
    """
    box: dict = {}

    def _runner():
        try:
            box['value'] = fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised in the caller
            box['error'] = exc

    worker = threading.Thread(target=_runner, name=f"deadline:{what}",
                              daemon=True)
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        raise timeout_error(
            f"{what} did not complete within {timeout:.0f}s and is still "
            f"running; it is blocked in a driver call that cannot be "
            f"interrupted"
        )
    if 'error' in box:
        raise box['error']
    return box.get('value')
