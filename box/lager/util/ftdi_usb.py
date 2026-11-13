# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Reopen an FTDI part that was re-plugged while this process was running.

pyusb keeps one libusb context per process (``usb.backend.libusb1``'s
``_lib_object``), and libusb keeps that context's device list current from
the kernel's hotplug events. Inside the box container those events do not
arrive, so after the part is unplugged and plugged back -- a hub port cycle,
a power cut, a cable pull -- the context's list keeps the old device (old bus
address) and never gains the new one. pyftdi adds a cache of its own on top,
``UsbTools.UsbDevices``. Every open in that process then fails, retries
included, until the process exits. Measured on an FT4232H behind a hub port
cycle: sysfs and a fresh libusb context saw the part at its new address, the
process's shared context still had the old one, and pyftdi's flush alone only
emptied it.

That is what broke ``lager gpi`` and ``lager nets state`` after a re-plug:
hardware_service lives for the life of the container, so the FTDI GPIO, I2C
and SPI nets all stayed unreachable until ``lager box-config restart``. A fresh
``lager python`` process starts with a fresh context and was never affected.

The failure shows up as several different errors depending on how far the
open got (``[Errno 19] No such device``, ``No USB device with S/N <sn>``, ``The
device has no langid``), so :func:`open_fresh` does not go by the message. When
an open fails it compares the FTDI devices the shared context lists with the
ones sysfs lists. Only if they differ does it create a fresh context, install
it as pyusb's shared one, flush pyftdi's cache and open once more. Otherwise
the failure is raised untouched.

**No libusb context is ever destroyed here.** pyusb finalizes a context and
the device lists it handed out when they are garbage-collected, in no fixed
order; ``libusb_exit`` before the last ``libusb_unref_device`` is a
use-after-free that took hardware_service down with a general protection
fault in libusb. So the check reads sysfs rather than opening a context to
compare against, and the context a refresh replaces is parked in
``_RETIRED`` for the life of the process. That costs one context per real
re-plug.

Kept free of pyftdi and pyusb at import time, like ``ftdi_url``, so the
drivers and their unit tests import without them.
"""

from __future__ import annotations

import glob
import os
import threading
from typing import Callable, List, Optional, Set, Tuple, TypeVar

T = TypeVar("T")

FTDI_VID = 0x0403
SYSFS_USB_DEVICES = "/sys/bus/usb/devices"

Address = Tuple[int, int, int]  # (pid, bus, address)

# Contexts replaced by a refresh. Never released: see the module docstring.
_RETIRED: List[object] = []
_refresh_lock = threading.Lock()


def _sysfs_ftdi_addresses() -> Optional[Set[Address]]:
    """FTDI devices as the kernel has them now, or None if sysfs is unreadable."""
    paths = glob.glob(os.path.join(SYSFS_USB_DEVICES, "*"))
    if not paths:
        return None
    found = set()
    for path in paths:
        try:
            with open(os.path.join(path, "idVendor")) as f:
                if int(f.read().strip(), 16) != FTDI_VID:
                    continue
            with open(os.path.join(path, "idProduct")) as f:
                pid = int(f.read().strip(), 16)
            with open(os.path.join(path, "busnum")) as f:
                bus = int(f.read().strip())
            with open(os.path.join(path, "devnum")) as f:
                address = int(f.read().strip())
        except (OSError, ValueError):
            continue  # interfaces, hubs' ports, a device mid-enumeration
        found.add((pid, bus, address))
    return found


def _context_ftdi_addresses(backend) -> Set[Address]:
    import usb.core
    return {(d.idProduct, d.bus, d.address)
            for d in usb.core.find(find_all=True, idVendor=FTDI_VID,
                                   backend=backend)}


def _shared_context_is_stale() -> bool:
    """True if pyusb's shared context lists different FTDI devices than sysfs.

    Creates no libusb context. Any error while checking counts as "not
    stale", so the caller raises the original open failure, not this one.
    """
    try:
        import usb.backend.libusb1 as libusb1
        shared = libusb1.get_backend()
        if shared is None:
            return False
        kernel = _sysfs_ftdi_addresses()
        if kernel is None:
            return False
        return _context_ftdi_addresses(shared) != kernel
    except Exception:
        return False


def _refresh_context() -> None:
    """Install a fresh libusb context as pyusb's shared one; flush pyftdi.

    The old context is retired, not released, and so are the devices other
    drivers still hold open on it.
    """
    import usb.backend.libusb1 as libusb1
    with _refresh_lock:
        old = libusb1.get_backend()
        fresh = libusb1._LibUSB(libusb1._lib)
        if old is not None:
            _RETIRED.append(old)
        libusb1._lib_object = fresh
    try:
        from pyftdi.usbtools import UsbTools
    except ImportError:
        return
    UsbTools.flush_cache()


def open_fresh(open_fn: Callable[[], T],
               log: Optional[Callable[[str], None]] = None) -> T:
    """Call ``open_fn``; if the USB device list was stale, refresh and retry once.

    ``open_fn`` must start from nothing each time: build a new controller and
    look the device up again, since a lookup made before the refresh is the
    stale one. A failure with an up-to-date device list, a failed refresh,
    and the second failure are all raised as the open's own error.
    """
    try:
        return open_fn()
    except Exception as exc:
        if not _shared_context_is_stale():
            raise
        if log is not None:
            log(f"open failed ({exc}); USB device list was stale, "
                "refreshing libusb and pyftdi and opening again")
        try:
            _refresh_context()
        except Exception as refresh_exc:
            if log is not None:
                log(f"refresh failed: {refresh_exc}")
            raise exc
    return open_fn()
