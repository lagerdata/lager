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

Heavily gated so it only fires for the real wedge: the device must be
enumerated in sysfs (a restart can help; an unplugged device can't, so we don't
loop) and we must not be inside a per-service cooldown.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import time
import traceback

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_S = 60.0

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


def usb_device_enumerated(address):
    """``True``/``False`` if the USB device is / isn't on the bus right now;
    ``None`` if unknown (non-USB address or sysfs unavailable).

    Reads sysfs (the kernel's device list) rather than libusb/PyUSB on purpose:
    the wedge is precisely that THIS process's USB context is stale and can't
    see the re-enumerated device, so a libusb-based check would wrongly report
    the device as gone and suppress the restart. sysfs is unaffected."""
    vid, pid, serial = usb_ids_from_address(address)
    if vid is None:
        return None
    vid_s, pid_s = f"{vid:04x}", f"{pid:04x}"
    try:
        for dev_dir in glob.glob("/sys/bus/usb/devices/*/"):
            def _read(name):
                try:
                    with open(os.path.join(dev_dir, name)) as fh:
                        return fh.read().strip()
                except OSError:
                    return None
            if (_read("idVendor") or "").lower() != vid_s:
                continue
            if (_read("idProduct") or "").lower() != pid_s:
                continue
            if serial is None:
                return True
            dev_serial = _read("serial")
            if dev_serial is None or dev_serial == serial:
                # serial matches, or is unreadable but VID/PID matched — present
                return True
        return False
    except Exception:
        return None


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
                       cooldown_s=DEFAULT_COOLDOWN_S):
    """Exit (so the supervisor respawns this service) when ``address``'s device
    is wedged in-process: enumerated in sysfs but unreachable. No-op when the
    device isn't on the bus (a restart can't help) or we self-restarted within
    the cooldown (anti-loop).

    ``service`` is a human label for logs; ``stamp_path`` is a per-service
    cooldown file so each service's cooldown is independent.
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
    logger.critical("[self-restart] %s: %s is enumerated but unreachable "
                    "in-process (orphaned USB claim). Exiting so the supervisor "
                    "respawns %s with a clean USB context.",
                    context, address, service)
    logging.shutdown()  # flush handlers before the hard exit
    os._exit(70)  # EX_SOFTWARE; start-services.sh's `while true` respawns us
