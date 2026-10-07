# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Shared self-restart helper for box services.

When a USB instrument re-enumerates (mains power-cycle / USB hub-port toggle),
the process's libusb/HID context can be orphaned: the device is back on the bus
(visible in sysfs) but THIS process can never reopen it — only a fresh process
can. Reproduced on a Keithley 2281S (pyvisa session, hardware_service) and a
YKUSH hub (pykush/HID, box_http_server). Both services run under
``start-services.sh``'s ``while true`` supervisor, so the reliable fix is to
exit and let the supervisor respawn the service with a clean USB context — the
next request/poll then works and the TUI self-heals (~2s blip, no container or
box restart).

The same orphaned context has a second, worse shape: the driver call does not
fail, it never returns (see ``util/watchdog.py``). That is the case
``schedule_self_restart_for_hang`` covers — a restart is even more clearly the
only recovery there, because the wedged native thread cannot be killed from
Python.

Heavily gated so it only fires for the real wedge: the device must be
enumerated in sysfs (a restart can help; an unplugged device can't, so we don't
loop) and we must not be inside a per-service cooldown.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import threading
import time
import traceback

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_S = 60.0

# How long to wait before the hang path runs its gate check. A hang has to do
# two things — tell the caller AND trigger the respawn — and doing the second
# inline costs the first: ``os._exit`` fires before the framework writes the
# response, so the client sees a dropped connection instead of the structured
# timeout error it needs to report. Deferring by a beat lets the response flush
# first. Not tunable per call site: every caller wants the same "after the
# response, before anyone retries" moment.
_HANG_RESTART_DELAY_S = 1.0

# Substrings that mark an exception/traceback as a failure to OPEN a session
# (vs. a normal command error on an already-open device). The joulescope
# markers cover the jsdrv backend's wedge signatures: open fails with an
# opaque -4, or the in-process scan stops seeing a device that sysfs still
# shows on the bus (maybe_self_restart's enumeration gate keeps a genuinely
# unplugged device from triggering a restart).
OPEN_FAILURE_MARKERS = (
    'open_resource', 'open_bare_resource', 'after_parsing',
    'could not open instrument',
    'jsdrv_open failed', 'failed to open joulescope',
    'joulescope with serial',
)

# Substrings that mark a USB-hub error as "can't reach the device" (a wedge
# candidate) vs. "the device responded with an error" (not a wedge). Used by
# box_http_server's /usb/command handler, where YKUSH/pykush and Acroname both
# surface unreachable-device errors as plain "... not found" / "no device".
USB_UNREACHABLE_MARKERS = (
    'not found', 'no device', 'no such device', 'enodev',
    'could not open', 'cannot open', 'could not connect',
    'no backend', 'unable to claim', 'device disconnected',
)


def usb_ids_from_address(address):
    """Parse ``(vid, pid, serial)`` from a USB VISA-style address, else
    ``(None, None, None)``. e.g. ``USB0::0x05E6::0x2281::4518305::INSTR`` ->
    ``(0x05E6, 0x2281, '4518305')``."""
    m = re.match(r'USB\d*::(0x[0-9A-Fa-f]+)::(0x[0-9A-Fa-f]+)::([^:]+)::',
                 str(address or ''))
    if not m:
        return (None, None, None)
    try:
        return (int(m.group(1), 16), int(m.group(2), 16), m.group(3))
    except ValueError:
        return (None, None, None)


def enumerated_usb_ids():
    """Every USB device on the bus right now, from ONE pass over sysfs.

    Returns ``{(vid, pid): [serial, ...]}`` with ints for the IDs and the serial
    string as the kernel reports it, or ``None`` for a device whose serial file
    is unreadable or absent. Returns ``None`` when sysfs could not be read.

    An EMPTY result is returned as ``{}``, and what it means is the caller's
    call: a container with no USB view lists nothing, so a caller that would
    report devices as unplugged on the strength of it should treat ``{}`` as
    unknown, as the /nets/state sweep does.

    Callers that check several addresses (the /nets/state sweep) read this once
    and match against it with ``usb_address_enumerated``, rather than globbing
    sysfs once per address.
    """
    found = {}
    try:
        for dev_dir in glob.glob("/sys/bus/usb/devices/*/"):
            def _read(name):
                try:
                    with open(os.path.join(dev_dir, name)) as fh:
                        return fh.read().strip()
                except OSError:
                    return None
            vid_s, pid_s = _read("idVendor"), _read("idProduct")
            if not vid_s or not pid_s:
                continue
            try:
                key = (int(vid_s, 16), int(pid_s, 16))
            except ValueError:
                continue
            found.setdefault(key, []).append(_read("serial"))
    except Exception:
        return None
    return found


def usb_address_enumerated(address, ids, *, port_slots=True):
    """``True``/``False`` if ``address``'s device is / isn't in ``ids``;
    ``None`` if that cannot be told (non-USB address, or ``ids`` is None).

    ``ids`` is ``enumerated_usb_ids()``'s result. A device whose serial is
    unreadable matches on VID/PID alone, and so does a topology-addressed one
    (a ``port-<path>`` slot where the serial would be, used for hubs whose
    serials are not unique): neither has a serial to compare.
    ``port_slots=False`` compares a ``port-`` slot as if it were a serial, which
    is what ``usb_device_enumerated`` has always done (so it never matches).
    """
    if ids is None:
        return None
    vid, pid, serial = usb_ids_from_address(address)
    if vid is None:
        return None
    serials = ids.get((vid, pid))
    if not serials:
        return False
    if serial is None or (port_slots and serial.startswith("port-")):
        return True
    return any(s is None or s == serial for s in serials)


def usb_device_enumerated(address):
    """``True``/``False`` if the USB device is / isn't on the bus right now;
    ``None`` if unknown (non-USB address or sysfs unavailable).

    Reads sysfs (the kernel's device list) rather than libusb/PyUSB on purpose:
    the wedge is precisely that THIS process's USB context is stale and can't
    see the re-enumerated device, so a libusb-based check would wrongly report
    the device as gone and suppress the restart. sysfs is unaffected."""
    if usb_ids_from_address(address)[0] is None:
        return None
    # port_slots=False keeps this function's long-standing answer for a
    # topology-addressed hub. Matching it on VID/PID would change when the
    # self-restart below is allowed to fire, which is a separate decision.
    return usb_address_enumerated(address, enumerated_usb_ids(),
                                  port_slots=False)


def looks_like_open_failure(exc):
    """True when an exception/traceback looks like a failure to OPEN a session
    (vs. a normal command error on an already-open device)."""
    blob = (traceback.format_exc() + ' ' + str(exc)).lower()
    return any(m in blob for m in OPEN_FAILURE_MARKERS)


def looks_like_device_unreachable(exc):
    """True when a USB-hub error looks like the device can't be reached/found
    (a wedge candidate) rather than the device responding with an error."""
    blob = (traceback.format_exc() + ' ' + str(exc)).lower()
    return any(m in blob for m in USB_UNREACHABLE_MARKERS)


def maybe_self_restart(address, context, *, service, stamp_path,
                       cooldown_s=DEFAULT_COOLDOWN_S, wedge="unreachable"):
    """Exit (so the supervisor respawns this service) when ``address``'s device
    is wedged in-process: enumerated in sysfs but unreachable. No-op when the
    device isn't on the bus (a restart can't help) or we self-restarted within
    the cooldown (anti-loop).

    ``service`` is a human label for logs; ``stamp_path`` is a per-service
    cooldown file so each service's cooldown is independent. ``wedge`` names
    the shape of the wedge for the logs — "unreachable" (the call failed) or
    "hung" (the call never returned); the gating is identical either way.
    """
    # Retry the sysfs check (~4s): the wedge is detected at the tail of a
    # re-enumeration, so the device may not be back in sysfs for a beat. A false
    # "not on the bus" would suppress the restart and leave the service dead.
    enumerated = None
    for _delay in (0.0, 0.5, 1.0, 1.0, 1.5):
        if _delay:
            time.sleep(_delay)
        enumerated = usb_device_enumerated(address)
        if enumerated is not False:  # True (present) or None (unknown) — stop
            break
    if enumerated is None:
        logger.warning("[self-restart] %s: cannot confirm USB enumeration for "
                       "%s; not restarting.", context, address)
        return
    if enumerated is False:
        logger.warning("[self-restart] %s: %s is not on the USB bus "
                       "(unplugged/off) — a restart can't help; surfacing the "
                       "error.", context, address)
        return
    now = time.time()
    try:
        last = os.path.getmtime(stamp_path)
    except OSError:
        last = 0.0
    if now - last < cooldown_s:
        logger.error("[self-restart] %s: %s wedged, but %s self-restarted %ds "
                     "ago (< %ds cooldown); surfacing the error instead of "
                     "restarting again.", context, address, service,
                     int(now - last), int(cooldown_s))
        return
    try:
        with open(stamp_path, 'w') as fh:
            fh.write(str(now))
    except OSError:
        pass
    logger.critical("[self-restart] %s: %s is enumerated but %s in-process "
                    "(orphaned USB claim). Exiting so the supervisor respawns "
                    "%s with a clean USB context.",
                    context, address, wedge, service)
    logging.shutdown()  # flush handlers before the hard exit
    os._exit(70)  # EX_SOFTWARE; start-services.sh's `while true` respawns us


def schedule_self_restart_for_hang(address, context, *, service, stamp_path,
                                   cooldown_s=DEFAULT_COOLDOWN_S,
                                   delay_s=_HANG_RESTART_DELAY_S):
    """Self-restart path for an operation that HUNG rather than raised.

    ``maybe_self_restart`` needs no exception — only an address and a reason —
    but every caller reached it from an ``except`` block, so the failure mode
    that most needs a restart never triggered one: a native driver call that
    never returns produces nothing to catch. This is that entry point.

    Runs on a short timer for two reasons: the caller can return its structured
    timeout response before ``os._exit`` lands (see ``_HANG_RESTART_DELAY_S``),
    and the sysfs gate inside ``maybe_self_restart`` sleeps up to ~4s, which no
    request thread should pay for.

    Returns the timer so tests can join it; callers ignore it.
    """
    timer = threading.Timer(
        delay_s, maybe_self_restart, args=(address, context),
        kwargs={'service': service, 'stamp_path': stamp_path,
                'cooldown_s': cooldown_s, 'wedge': 'hung'},
    )
    timer.daemon = True
    timer.name = f"self-restart-hang:{service}"
    timer.start()
    return timer
