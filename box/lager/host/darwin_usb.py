# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""USB enumeration on macOS, for the hardware service's self-restart gate.

The hardware service recovers from a wedged USB session by exiting so its
supervisor starts a clean process -- but only when the device is still on
the bus, because a restart cannot bring back an unplugged instrument. On
Linux it answers "is it on the bus?" from sysfs (``lager.util.self_restart``).
macOS has no sysfs, so that check always said "not on the bus" and the
recovery could never fire.

This module answers the same question from the I/O Registry. Like the sysfs
read it replaces, it deliberately does not go through libusb: the wedge is
that this process's own USB context is stale, so asking it would report the
device missing at exactly the moment the answer matters. ``ioreg`` runs as a
separate process and reads the kernel's view.

Only the agent uses this. The Linux implementation is not modified; on macOS
the agent swaps this function in for it (see ``install``).
"""
from __future__ import annotations

import logging
import plistlib
import subprocess
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

IOREG_COMMAND = ("/usr/sbin/ioreg", "-p", "IOUSB", "-l", "-a", "-w0")
IOREG_TIMEOUT_S = 5.0

_SERIAL_KEYS = ("USB Serial Number", "kUSBSerialNumberString")

UsbIds = Dict[Tuple[int, int], List[Optional[str]]]


def parse_ioreg(plist_bytes: bytes) -> UsbIds:
    """``{(vid, pid): [serial, ...]}`` from ``ioreg -p IOUSB -l -a`` output.

    Same shape as ``lager.util.self_restart.enumerated_usb_ids``: integer IDs,
    and ``None`` for a device that reports no serial number.
    """
    found: UsbIds = {}
    if not plist_bytes.strip():
        return found  # ioreg prints nothing when the plane is empty

    def walk(node) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        vid, pid = node.get("idVendor"), node.get("idProduct")
        # bool is an int subclass; a registry entry never uses it for an ID.
        if (isinstance(vid, int) and isinstance(pid, int)
                and not isinstance(vid, bool) and not isinstance(pid, bool)):
            serial = next((node[k] for k in _SERIAL_KEYS
                           if isinstance(node.get(k), str) and node[k]), None)
            found.setdefault((vid, pid), []).append(serial)
        walk(node.get("IORegistryEntryChildren"))

    walk(plistlib.loads(plist_bytes))
    return found


def enumerated_usb_ids() -> Optional[UsbIds]:
    """Every USB device on the bus right now, or ``None`` if that is unknown.

    ``None`` -- not an empty result -- when ``ioreg`` cannot be run or read.
    The caller treats ``None`` as "cannot tell" and does not restart, whereas
    an empty result means "nothing is plugged in".
    """
    try:
        out = subprocess.run(IOREG_COMMAND, capture_output=True, check=True,
                             timeout=IOREG_TIMEOUT_S).stdout
        return parse_ioreg(out)
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown"
        logger.warning("Could not read the USB device list from ioreg: %s", exc)
        return None


def parse_devices(plist_bytes: bytes) -> List[Dict[str, Optional[str]]]:
    """One entry per USB device, in the shape ``lager.util.usb_sysfs`` returns.

    ``sysfs_name`` is the device's location ID: like a sysfs name it follows
    the port a device is plugged into, so it is the same before and after a
    power cycle. That is what the hub drivers compare to tell that a cycled
    device came back.
    """
    devices: List[Dict[str, Optional[str]]] = []
    if not plist_bytes.strip():
        return devices

    def text(value) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    def number(value) -> Optional[int]:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def walk(node) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        vid, pid = number(node.get("idVendor")), number(node.get("idProduct"))
        location = number(node.get("locationID"))
        if vid is not None and pid is not None and location is not None:
            address = number(node.get("USB Address"))
            device_class = number(node.get("bDeviceClass"))
            devices.append({
                "sysfs_name": f"{location:08x}",
                "vid": f"{vid:04x}",
                "pid": f"{pid:04x}",
                "serial": next((node[k] for k in _SERIAL_KEYS if text(node.get(k))), None),
                "product": text(node.get("USB Product Name")),
                "manufacturer": text(node.get("USB Vendor Name")),
                "busnum": None,
                "devnum": None if address is None else str(address),
                "devpath": None,
                "device_class": None if device_class is None else f"{device_class:02x}",
                "speed": None,
            })
        walk(node.get("IORegistryEntryChildren"))

    walk(plistlib.loads(plist_bytes))
    return devices


def enumerate_usb_devices(sysfs_root=None, vid=None, pid=None, serial=None):
    """``lager.util.usb_sysfs.enumerate_usb_devices``, from the I/O Registry.

    Same filters and the same meaning of an empty result: the bus could not
    be read. ``sysfs_root`` is accepted and ignored.
    """
    from lager.util.usb_sysfs import _norm_hex_id

    try:
        out = subprocess.run(IOREG_COMMAND, capture_output=True, check=True,
                             timeout=IOREG_TIMEOUT_S).stdout
        devices = parse_devices(out)
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown"
        logger.warning("Could not read the USB device list from ioreg: %s", exc)
        return []
    want_vid, want_pid = _norm_hex_id(vid), _norm_hex_id(pid)
    return [d for d in devices
            if (not want_vid or _norm_hex_id(d["vid"]) == want_vid)
            and (not want_pid or _norm_hex_id(d["pid"]) == want_pid)
            and (not serial or d["serial"] == serial)]


def install() -> None:
    """Make the hardware service's self-restart gate use this enumeration.

    Replaces one function on the shared module rather than editing it, so a
    Linux Box runs exactly the code it ran before. ``test_host_darwin_usb.py``
    pins that the gate still looks the function up this way.
    """
    from lager.util import self_restart

    self_restart.enumerated_usb_ids = enumerated_usb_ids
    logger.info("USB enumeration for self-restart: I/O Registry (macOS)")

    # The hub drivers confirm a power cycle from the bus. Each of these bound
    # the sysfs reader by name at import, so each is given the replacement.
    from lager.automation.usb_hub import acroname, usb_net
    from lager.util import usb_sysfs

    for module in (usb_sysfs, usb_net, acroname):
        module.enumerate_usb_devices = enumerate_usb_devices
