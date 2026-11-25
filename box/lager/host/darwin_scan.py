# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Instrument discovery on macOS, using the Box's own scanner.

``lager instruments`` is answered by ``lager.http_handlers.usb_scanner``,
which reads the USB bus from ``/sys/bus/usb/devices``. A Mac has no sysfs,
and the scanner's rules -- which device is which instrument, how a hub with
a shared serial is addressed, which half of a dock is listed -- are too many
to keep a second copy of in step.

So the scanner is left as it is and given something to read: before each
scan the Mac's USB bus (from the I/O Registry) is written out as a small
directory tree laid out like sysfs, and the scanner's ``/sys/bus/usb/devices``
paths are pointed at it. The scanner file itself is not edited.

A device's directory name is built from its location ID the way Linux builds
one from the port path: location ``0x01114400`` is bus 1, ports 1, 1, 4, 4,
so ``1-1.1.4.4``. Like the Linux name it follows the socket, not the device.

Serial ports are written out too, as ``/sys/class/tty`` entries under the
Linux names ``lager.host.darwin_serial`` gives them, so the Box's code finds
which USB device and interface a port belongs to the way it does on Linux.

What this does not provide: ``/sys/class/video4linux``. Cameras are therefore
not listed yet.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import pathlib
import shutil
import tempfile
import threading

from . import darwin_serial, darwin_usb

logger = logging.getLogger(__name__)

SYSFS_USB = "/sys/bus/usb/devices"
SYSFS_TTY = "/sys/class/tty"
_ATTRIBUTES = (("idVendor", "vid"), ("idProduct", "pid"), ("serial", "serial"),
               ("product", "product"), ("manufacturer", "manufacturer"))

_lock = threading.Lock()
_scanner = None
_root = None


def sysfs_name(location: int):
    """The Linux-style name for a location ID, or None for a root hub."""
    bus = (location >> 24) & 0xFF
    ports = []
    for shift in range(20, -4, -4):
        nibble = (location >> shift) & 0xF
        if nibble == 0:
            break
        ports.append(str(nibble))
    if not ports:
        return None
    return f"{bus}-{'.'.join(ports)}"


def _device_dir(root: str, name: str) -> str:
    """Where the device called ``name`` sits: inside each hub above it."""
    bus, _, path = name.partition("-")
    # Each bus under a controller of its own, with its root hub.
    directory = os.path.join(root, "tree", f"controller{bus}", f"usb{bus}")
    parts = path.split(".")
    for depth in range(1, len(parts) + 1):
        directory = os.path.join(directory, f"{bus}-{'.'.join(parts[:depth])}")
    return directory


def render(devices, root: str, ports=(), names=None) -> None:
    """Write the bus under ``root`` as sysfs would show it.

    ``devices`` is ``darwin_usb.parse_devices``'s list. ``root/devices`` holds
    one link per device, as ``/sys/bus/usb/devices`` does, into a tree where a
    device's directory sits inside its parent hub's. The scanner reads that
    nesting to tell a cascaded hub tier.

    ``ports`` is ``darwin_serial.parse_ports``'s list and ``names`` maps each
    port's Mac device to its Linux name. Each becomes ``root/class/tty/<name>``
    with a ``device`` link into its USB interface, as ``/sys/class/tty`` has.
    """
    shutil.rmtree(root, ignore_errors=True)
    listing = os.path.join(root, "devices")
    ttys = os.path.join(root, "class", "tty")
    os.makedirs(listing, mode=0o700)
    os.makedirs(ttys)
    for device in devices:
        try:
            name = sysfs_name(int(device["sysfs_name"], 16))
        except (TypeError, ValueError):
            continue
        if name is None:
            continue
        bus = name.partition("-")[0]
        directory = _device_dir(root, name)
        os.makedirs(directory, exist_ok=True)
        for filename, key in _ATTRIBUTES:
            value = device.get(key)
            if value is not None:
                with open(os.path.join(directory, filename), "w", encoding="utf-8") as fh:
                    fh.write(f"{value}\n")
        bus_root = os.path.join(root, "tree", f"controller{bus}", f"usb{bus}")
        for link, target in ((f"usb{bus}", bus_root), (name, directory)):
            link_path = os.path.join(listing, link)
            if not os.path.lexists(link_path):
                os.symlink(target, link_path)
    for port in ports:
        tty = (names or {}).get(port.get("device"))
        name = sysfs_name(port["location"]) if tty else None
        if name is None or not os.path.isdir(_device_dir(root, name)):
            continue
        interface = port.get("interface") or 0
        port_dir = os.path.join(_device_dir(root, name), f"{name}:1.{interface}", tty)
        os.makedirs(port_dir, exist_ok=True)
        os.makedirs(os.path.join(ttys, tty))
        os.symlink(port_dir, os.path.join(ttys, tty, "device"))


def _redirected_path(root: str):
    """``Path`` for Box code: the sysfs paths it reads land under ``root``."""
    moved = ((SYSFS_USB, os.path.join(root, "devices")),
             (SYSFS_TTY, os.path.join(root, "class", "tty")))

    def path(*args):
        real = pathlib.Path(*args)
        text = str(real)
        for prefix, target in moved:
            if text == prefix or text.startswith(prefix + "/"):
                return pathlib.Path(target + text[len(prefix):])
        return real

    return path


def _load_scanner(root: str):
    """The Box's scanner module, reading ``root`` where it would read sysfs.

    Loaded from its file under a private name: importing it as part of
    ``lager.http_handlers`` would start the Box's HTTP handlers, which the
    agent neither runs nor has the dependencies for.
    """
    import lager

    source = os.path.join(os.path.dirname(lager.__file__),
                          "http_handlers", "usb_scanner.py")
    spec = importlib.util.spec_from_file_location("lager_host_usb_scanner", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.Path = _redirected_path(root)
    module._SYS_USB_DEVICES = pathlib.Path(root, "devices")
    return module


def default_root() -> str:
    return os.path.join(tempfile.gettempdir(), f"lager-host-{os.getuid()}", "sysfs")


def install_serial(root=None) -> None:
    """Point the Box's serial-port lookups at the tree, in this process.

    Only the agent calls this. ``serial_id`` keeps its sysfs location in one
    name; the UART bridge spells the path out, so it gets the redirecting
    ``Path`` the scanner gets.
    """
    global _root
    from lager.devices import serial_id
    from lager.protocols.uart import uart_bridge

    with _lock:
        _root = _root or root or default_root()
        serial_id._SYS_TTY = pathlib.Path(_root, "class", "tty")
        uart_bridge.Path = _redirected_path(_root)


def _refresh_locked() -> None:
    ports = darwin_serial.enumerate_ports()
    names = darwin_serial.NAMES.sync(port["device"] for port in ports)
    render(darwin_usb.enumerate_usb_devices(), _root, ports, names)


def refresh(root=None) -> None:
    """Rewrite the tree from the bus as it is now."""
    global _root
    with _lock:
        _root = _root or root or default_root()
        _refresh_locked()


def list_instruments(root=None):
    """What ``usb_scanner.list_instruments`` returns on a Linux Box."""
    global _scanner, _root
    with _lock:
        _root = _root or root or default_root()
        if _scanner is None:
            _scanner = _load_scanner(_root)
        _refresh_locked()
        return _scanner.list_instruments()
