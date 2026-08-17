# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Wait for a net to become usable after the hardware underneath it moves.

Enumeration is not readiness. Cutting AC power to an instrument, or toggling
the USB hub port it sits on, produces a sequence rather than an event:

1. the instrument disappears from the bus,
2. it reappears and enumerates,
3. the USB hotplug restarts the box's hardware service,
4. the service accepts connections again,
5. the instrument answers.

A script that starts at step 1 and calls a net immediately gets
``Device not found`` at step 2, ``Connection refused`` at step 3, and only
works from step 5. Those look like hardware faults and are not.

``integration-tests.yml`` learned this the hard way and polls twice in its
power-on step -- once for enumeration, once for a real supply read -- but that
knowledge lived only in the workflow. Anything run by hand, or any suite
invoked outside it, rediscovers it as a confusing failure. This is that gate,
somewhere a script can call it.

    from lager import Net, NetType
    from lager.util.net_ready import wait_for_net

    if not wait_for_net("supply3", NetType.PowerSupply):
        raise SystemExit("supply3 never came back")

Deliberately returns a bool rather than raising: the caller decides whether a
net that never appears is fatal. A test suite usually wants to skip; an
interactive script usually wants to stop.
"""

import time

__all__ = ["wait_for_net", "DEFAULT_TIMEOUT_S"]

# Sized against the slowest instrument on a Lager bench: a Keithley 2281S
# measures ~90s from AC power-on to enumeration. 180s leaves room for the
# hardware-service restart that follows the USB hotplug without waiting out a
# genuinely absent instrument for long enough to matter.
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_INTERVAL_S = 5.0


def _default_probe(net):
    """Cheapest call that proves the net answers, whatever type it is.

    ``get_config`` is served from the net record and does NOT touch the
    instrument, so it cannot prove readiness. These do.
    """
    for attr in ("voltage", "input", "state", "get_config"):
        fn = getattr(net, attr, None)
        if callable(fn):
            return fn()
    raise AttributeError("no probe method on this net; pass probe=")


def wait_for_net(name, net_type=None, *, timeout=DEFAULT_TIMEOUT_S,
                 interval=DEFAULT_INTERVAL_S, probe=None, on_wait=None):
    """Poll ``name`` until it answers, or ``timeout`` expires.

    Returns True once a probe call succeeds, False if the deadline passes.

    ``probe`` overrides what counts as answering; it is called with the
    resolved net. ``on_wait`` is called with (attempt, elapsed, exception) each
    time a probe fails, so a caller can print progress without this module
    deciding how.

    Every exception is treated as not-ready-yet rather than fatal. That is
    deliberate and worth stating: the failure modes across this window are
    varied and not usefully distinguishable -- ``DeviceNotFoundError`` while
    the bus settles, ``ConnectionFailed`` or a bare ``RemoteDisconnected``
    while the hardware service restarts, and assorted HTTP errors in between.
    Enumerating them would mean re-listing them every time a driver grows a new
    one, and getting that list wrong turns a recoverable wait into a crash.
    The deadline is what bounds this, not the exception type.
    """
    from lager import Net

    deadline = time.monotonic() + timeout
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            net = Net.get(name, type=net_type) if net_type else Net.get(name)
            (probe or _default_probe)(net)
            return True
        except Exception as e:  # noqa: BLE001 -- see docstring
            if time.monotonic() >= deadline:
                return False
            if on_wait is not None:
                try:
                    on_wait(attempt, time.monotonic() - start, e)
                except Exception:  # noqa: BLE001 -- progress must not break the wait
                    pass
            # Sleep no further than the deadline, so a long interval cannot
            # overshoot a short timeout.
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
