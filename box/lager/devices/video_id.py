# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Durable identity for USB video devices.

A ``/dev/videoN`` number is **not stable**: it depends on the order the box
enumerates devices. A webcam net therefore finds its camera again from its
address when it is opened, and the number saved with the net is only used when
the address cannot identify one camera.

Address schemes:

    USB0::0x<vid>::0x<pid>::<serial>::INSTR  # a camera with a serial of its own
    usb-port:<sysfs-port-path>               # no serial, or one it shares

A port address pins the net to a physical USB port, so moving the camera breaks
it. That is the intended trade-off for cameras nothing else can tell apart, and
the same one capture cards and serial-less cables make.

This reads sysfs only. It runs during instrument scans, and opening a camera
could disturb a stream in progress.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

V4L_ROOT = Path("/sys/class/video4linux")

PORT_ADDRESS_PREFIX = "usb-port:"

_VIDEO_CLASS = "0e"
_NODE_NAME = re.compile(r"video(\d+)")
_VISA_ADDRESS = re.compile(
    r"USB0::0x([0-9a-f]{4})::0x([0-9a-f]{4})::([^:]*)::INSTR", re.IGNORECASE)


class VideoDeviceNotFound(RuntimeError):
    """The camera a net names is not on the box."""


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def _node_index(node: Path) -> int:
    """The node's index on its device. A missing file counts as 0."""
    try:
        return int(_read(node / "index") or 0)
    except ValueError:
        return 0


def usb_video_devices(v4l_root: Path = V4L_ROOT) -> List[dict]:
    """Every USB device with a video node, one record each.

    Each ``videoN`` links to the USB interface that owns it, and the
    interface's parent directory is the USB device. A device registers several
    nodes; the lowest-numbered with index 0 is its capture stream, and the
    others carry metadata. Nodes with no USB device behind them are skipped.

    ``uvc`` is False for an interface that is not USB video class. A node can
    belong to one, such as a vendor-specific grabber, without being a camera a
    net can use.
    """
    numbered = []
    for node in v4l_root.glob("video*"):
        match = _NODE_NAME.fullmatch(node.name)
        if match:
            numbered.append((int(match.group(1)), node))

    devices: List[dict] = []
    seen: set = set()
    for _, node in sorted(numbered, key=lambda item: item[0]):
        try:
            interface = (node / "device").resolve()
        except OSError:
            continue
        usb_dev = interface.parent
        vid = _read(usb_dev / "idVendor")
        pid = _read(usb_dev / "idProduct")
        if not vid or not pid:
            continue
        if str(usb_dev) in seen or _node_index(node) != 0:
            continue
        seen.add(str(usb_dev))
        devices.append({
            "node": f"/dev/{node.name}",
            "vid": vid.lower(),
            "pid": pid.lower(),
            "serial": _read(usb_dev / "serial"),
            "product": _read(usb_dev / "product"),
            "port_path": usb_dev.name,
            "uvc": (_read(interface / "bInterfaceClass") or "").lower() == _VIDEO_CLASS,
        })
    return devices


def resolve_video_device(address: Optional[str], saved: str,
                         v4l_root: Path = V4L_ROOT) -> str:
    """The device node to open for a webcam net.

    *address* is the net's address and *saved* the node it was created with.
    A missing camera raises rather than falling back to *saved*, which by then
    may be a different camera. *saved* is returned only when the address cannot
    name one camera: it has no serial, several cameras share its serial, or it
    is in neither scheme.
    """
    address = address or ""
    if address.startswith(PORT_ADDRESS_PREFIX):
        port = address[len(PORT_ADDRESS_PREFIX):]
        devices = usb_video_devices(v4l_root)
        for device in devices:
            if device["port_path"] == port:
                return device["node"]
        ports = sorted({device["port_path"] for device in devices})
        elsewhere = ("video devices are on USB ports %s" % ", ".join(ports)
                     if ports else "no video device is on any USB port")
        raise VideoDeviceNotFound(
            "No camera is on USB port %s; check that it is plugged into the "
            "port the net was created with (%s)" % (port, elsewhere))

    match = _VISA_ADDRESS.fullmatch(address)
    if not match or not match.group(3):
        return saved
    vid, pid, serial = match.group(1).lower(), match.group(2).lower(), match.group(3)
    found = [device for device in usb_video_devices(v4l_root)
             if (device["vid"], device["pid"], device["serial"]) == (vid, pid, serial)]
    if len(found) == 1:
        return found[0]["node"]
    if not found:
        raise VideoDeviceNotFound(
            "Camera %s:%s with serial %s is not on the box; check that it is "
            "plugged in" % (vid, pid, serial))
    return saved
