# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""USB serial ports on macOS, under the names a Linux Box gives them.

A Mac calls a USB serial adapter ``/dev/cu.usbserial-0001``; a Linux Box
calls it ``/dev/ttyUSB0``, and that is the name saved nets, scripts and the
Box's own code use. So the agent gives each Mac port a Linux name -- ``ttyUSB``
for an adapter, ``ttyACM`` for a CDC device, numbered in the order they
appear, lowest free number first, as the Linux kernel does -- and keeps the
table that maps one to the other. The container only ever sees the Linux name.

Like a Linux name, the number is not durable across replugging. What is
durable is the USB identity (IDs, serial, port, interface), which is what a
saved net records.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import plistlib
import re
import subprocess
import threading
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Every USB device with everything attached below it: its interfaces, and
# the serial ports the drivers bound to them.
IOREG_COMMAND = ("/usr/sbin/ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-a", "-w0")
IOREG_TIMEOUT_S = 5.0
DEV_PREFIX = "/dev/"
# The names this module hands out. Only these are translated on open.
LINUX_PORT = re.compile(r"^/dev/tty(USB|ACM)[0-9]+$")


def _number(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_ports(plist_bytes: bytes) -> List[Dict]:
    """``[{"device", "location", "interface"}, ...]`` for each USB serial port.

    ``device`` is the Mac's callout device (``/dev/cu.*``), ``location`` the
    location ID of the USB device it belongs to, and ``interface`` the USB
    interface number, or None when the registry does not say.
    """
    ports: Dict[str, Dict] = {}
    if not plist_bytes.strip():
        return []

    def walk(node, location, interface) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, location, interface)
            return
        if not isinstance(node, dict):
            return
        if _number(node.get("idVendor")) is not None and _number(node.get("locationID")) is not None:
            location, interface = node["locationID"], None
        elif _number(node.get("bInterfaceNumber")) is not None:
            interface = node["bInterfaceNumber"]
        device = node.get("IOCalloutDevice")
        if isinstance(device, str) and device.startswith(DEV_PREFIX) and location is not None:
            ports[device] = {"device": device, "location": location, "interface": interface}
        walk(node.get("IORegistryEntryChildren"), location, interface)

    walk(plistlib.loads(plist_bytes), None, None)
    return [ports[device] for device in sorted(ports)]


def enumerate_ports() -> List[Dict]:
    """The Mac's USB serial ports right now; empty if that cannot be read."""
    try:
        out = subprocess.run(IOREG_COMMAND, capture_output=True, check=True,
                             timeout=IOREG_TIMEOUT_S).stdout
        return parse_ports(out)
    except Exception as exc:  # noqa: BLE001 - any failure means "none seen"
        logger.warning("Could not read the serial port list from ioreg: %s", exc)
        return []


def _kind(device: str) -> str:
    return "ttyACM" if "usbmodem" in device else "ttyUSB"


class Names:
    """The table of Linux names for the Mac's serial ports."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_device: Dict[str, str] = {}

    def sync(self, devices) -> Dict[str, str]:
        """Forget ports that left, name ports that arrived; ``{device: name}``."""
        with self._lock:
            present = set(devices)
            for device in [d for d in self._by_device if d not in present]:
                del self._by_device[device]
            for device in sorted(present - set(self._by_device)):
                kind = _kind(device)
                taken = set(self._by_device.values())
                number = 0
                while f"{kind}{number}" in taken:
                    number += 1
                self._by_device[device] = f"{kind}{number}"
            return dict(self._by_device)

    def host_device(self, tty: str) -> Optional[str]:
        """The Mac device behind ``/dev/ttyUSB0`` (or ``ttyUSB0``), or None."""
        name = tty[len(DEV_PREFIX):] if tty.startswith(DEV_PREFIX) else tty
        with self._lock:
            for device, linux_name in self._by_device.items():
                if linux_name == name:
                    return device
        return None

    def linux_paths(self) -> List[str]:
        """Every port's Linux path (``/dev/ttyUSB0``, ...), sorted."""
        with self._lock:
            return sorted(DEV_PREFIX + name for name in self._by_device.values())


NAMES = Names()


def glob_ports(pattern: str, names: Optional[Names] = None) -> List[str]:
    """What ``glob.glob(pattern)`` would return on a Linux Box, sorted.

    Answers ``/dev/ttyUSB*``-style patterns from the name table. No other
    ``/dev`` path exists under a Linux name here, so any other pattern
    (``/dev/serial/by-id/*``) matches nothing.
    """
    return [path for path in (names or NAMES).linux_paths()
            if fnmatch.fnmatchcase(path, pattern)]


def port_exists(path: str, names: Optional[Names] = None) -> bool:
    """Whether ``path`` is attached: a Linux name by the table, else on disk."""
    if isinstance(path, str) and LINUX_PORT.match(path):
        return (names or NAMES).host_device(path) is not None
    return os.path.exists(path)


def install_pyserial(refresh: Optional[Callable[[], None]] = None,
                     names: Optional[Names] = None) -> None:
    """Make ``serial.Serial`` open a Linux name on the Mac port behind it.

    Only the agent calls this. Its drivers (a serial supply's VISA ASRL
    resource, which pyvisa-py opens through pySerial, or a driver calling
    ``serial.Serial("/dev/ttyACM0")``) name ports as on a Linux Box. The
    name is looked up in the table when the port is opened, after
    ``refresh`` has re-read the bus, and the Mac device is opened in its
    place. The port keeps its Linux name for everything else, including
    pySerial's own error messages. Any other path is opened unchanged.
    """
    import serial

    if getattr(serial.Serial, "_lager_host_names", False):
        return
    table = names or NAMES
    native = serial.Serial

    class Serial(native):
        _lager_host_names = True

        def open(self):
            linux_path = self.portstr
            if not (isinstance(linux_path, str) and LINUX_PORT.match(linux_path)):
                return super().open()
            if refresh is not None:
                try:
                    refresh()
                except Exception:  # noqa: BLE001 - the last table still answers
                    logger.exception("could not re-read the serial ports")
            device = table.host_device(linux_path)
            if device is None:
                # What pySerial reports on Linux for a port that is not there.
                raise serial.SerialException(
                    2, f"could not open port {linux_path}: [Errno 2] "
                       f"No such file or directory: {linux_path!r}")
            # pySerial opens ``portstr``; its messages name ``port``.
            self.portstr = device
            try:
                return super().open()
            finally:
                self.portstr = linux_path

    serial.Serial = Serial
