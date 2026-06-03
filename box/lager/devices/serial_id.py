# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Durable identity for serial (RS-232-over-USB) custom instruments.

A custom serial instrument (e.g. a Rigol DP711 behind a Prolific USB-serial
cable) is reached through a ``/dev/ttyUSB*`` node whose number is **not stable**
across reboots or replugs. To make a saved net survive renumbering we store a
*durable* identity in the net's address and resolve it to the live tty at open
time.

Address scheme (``serial://`` resources):

    serial://<vid>:<pid>/serial/<usb-serial>     # preferred when the cable has one
    serial://<vid>:<pid>/port/<sysfs-port-path>  # fallback when it has no serial

Resolution honors the project decision: **match by USB serial when the record
carries one, else by USB topology (port) path**. The port-path form pins the
net to a physical box port — moving the cable breaks it — which is the intended
trade-off for serial-less adapters.

NOTE: the box already has three other sysfs tty walks (``usb_scanner._walk_ttys``,
its ``query_instruments`` mirror, and ``uart_bridge._find_device_by_serial``).
Those resolve *all* interfaces of multi-channel chips for the scanner/UART
paths; this is a deliberately small single-cable resolver for the custom-device
framework. A future cleanup could unify all four behind one helper.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote, unquote

_SYS_TTY = Path("/sys/class/tty")
_SCHEME = "serial://"
# e.g. "serial://067b:23a3/serial/00000006"
_ADDR_RE = re.compile(
    r"^serial://(?P<vid>[0-9a-fA-F]{4}):(?P<pid>[0-9a-fA-F]{4})/(?P<kind>serial|port)/(?P<value>.+)$"
)


# ------------------------------ address codec ------------------------------

def is_serial_address(address: Optional[str]) -> bool:
    """True if *address* is a durable ``serial://`` custom-device resource."""
    return bool(address) and address.strip().lower().startswith(_SCHEME)


def make_address(vid: str, pid: str, serial: Optional[str] = None,
                 port_path: Optional[str] = None) -> str:
    """Build a durable ``serial://`` address.

    Prefers the USB serial when present; otherwise pins to the USB port path.
    Raises ValueError if neither identifier is available.
    """
    vid = (vid or "").lower()
    pid = (pid or "").lower()
    if serial:
        return f"{_SCHEME}{vid}:{pid}/serial/{quote(str(serial), safe='')}"
    if port_path:
        return f"{_SCHEME}{vid}:{pid}/port/{quote(str(port_path), safe='')}"
    raise ValueError("make_address requires either a serial number or a port path")


def parse_address(address: Optional[str]) -> Optional[Dict[str, Optional[str]]]:
    """Parse a ``serial://`` address into its parts, or None if not one.

    Returns {"vid", "pid", "serial", "port_path"} with exactly one of
    serial/port_path populated.
    """
    if not address:
        return None
    m = _ADDR_RE.match(address.strip())
    if not m:
        return None
    value = unquote(m.group("value"))
    kind = m.group("kind")
    return {
        "vid": m.group("vid").lower(),
        "pid": m.group("pid").lower(),
        "serial": value if kind == "serial" else None,
        "port_path": value if kind == "port" else None,
    }


# ------------------------------ tty resolution -----------------------------

def _read_sysfs_text(path: Path) -> Optional[str]:
    """Best-effort non-blocking read of a sysfs string attribute."""
    if not path.exists():
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
        try:
            return os.read(fd, 256).decode("utf-8").strip()
        finally:
            os.close(fd)
    except (OSError, UnicodeDecodeError, BlockingIOError):
        return None


def _usb_device_dir_for_tty(tty_dev: Path) -> Optional[Path]:
    """Walk a tty's parent chain to the owning USB device dir (has idVendor)."""
    device_link = tty_dev / "device"
    if not device_link.exists():
        return None
    try:
        node = device_link.resolve()
    except OSError:
        return None
    for _ in range(10):
        try:
            if (node / "idVendor").exists():
                return node
        except OSError:
            return None
        parent = node.parent
        if not parent or parent == node or parent == Path("/sys"):
            return None
        node = parent
    return None


def resolve_tty(vid: str, pid: str, serial: Optional[str] = None,
                port_path: Optional[str] = None) -> Optional[str]:
    """Return the live ``/dev/ttyUSB*`` (or ttyACM) for a cable identity.

    Matches the USB device whose idVendor/idProduct equal *vid*/*pid* and
    whose USB serial equals *serial* (when given) — otherwise whose sysfs
    port-path basename equals *port_path*. Returns None if no such cable is
    currently plugged in.
    """
    vid = (vid or "").lower()
    pid = (pid or "").lower()
    if not _SYS_TTY.exists():
        return None

    for tty_dev in sorted(_SYS_TTY.iterdir(), key=lambda p: p.name):
        if not tty_dev.name.startswith(("ttyUSB", "ttyACM")):
            continue
        usb_dir = _usb_device_dir_for_tty(tty_dev)
        if usb_dir is None:
            continue

        dev_vid = (_read_sysfs_text(usb_dir / "idVendor") or "").lower()
        dev_pid = (_read_sysfs_text(usb_dir / "idProduct") or "").lower()
        if dev_vid != vid or dev_pid != pid:
            continue

        if serial:
            if _read_sysfs_text(usb_dir / "serial") == serial:
                return f"/dev/{tty_dev.name}"
        elif port_path:
            if usb_dir.name == port_path:
                return f"/dev/{tty_dev.name}"

    return None


def resolve_address_to_tty(address: str) -> Optional[str]:
    """Resolve a ``serial://`` address straight to the live tty, or None."""
    parts = parse_address(address)
    if not parts:
        return None
    return resolve_tty(
        parts["vid"], parts["pid"],
        serial=parts.get("serial"), port_path=parts.get("port_path"),
    )
