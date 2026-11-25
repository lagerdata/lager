# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The ``/dev/serial/by-id`` and ``by-path`` names a Linux Box's udev gives.

On Linux, udev links each USB serial port under two durable names, and a
saved net may name its port by either. A Mac has no udev, so the agent works
the names out from the same USB identity udev reads.

``by-id`` follows the device: ``usb-<ID_SERIAL>-if<NN>``, plus ``-port<N>``
for a usb-serial adapter (``ttyUSB``; a CDC-ACM device has none). It is
built here exactly as udev 252 (Debian 12, the Box image) builds it in
``60-serial.rules`` and its ``usb_id`` builtin, so a by-id net saved on a
Linux Box names the same device on a Mac.

``by-path`` follows the socket, and on Linux starts with the machine's own
USB controller (``pci-0000:00:14.0-usb-0:1.2:1.0``), so a by-path name from one
machine means nothing on another. The Mac gets by-path names of its own, in
the same shape, with ``macos-usb<bus>`` where Linux names the controller:
``platform-macos-usb1-usb-0:1.2:1.0-port0``. A net made on the Mac can be
pinned to a socket; one copied from a Linux Box cannot, and is told so.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional

BY_ID = "/dev/serial/by-id/"
BY_PATH = "/dev/serial/by-path/"
MAC_PATH_PREFIX = "platform-macos-usb"

# udev's buffers for the vendor and model strings (char[64], one byte for NUL).
_STRING_LIMIT = 63
# What udev keeps in a device name, besides letters and digits.
_ALLOWED = frozenset(b"#+-.:=@_")
_WHITESPACE = frozenset(b" \t\n\v\f\r")


def _replace_whitespace(raw: bytes, limit: int) -> bytes:
    """udev_replace_whitespace: trim, and each whitespace run becomes ``_``.

    Like udev, reads at most ``limit`` bytes of the input.
    """
    out = bytearray()
    i = 0
    while i < len(raw) and raw[i] in _WHITESPACE:
        i += 1
    pending_space = False
    while len(out) < limit and i < limit and i < len(raw):
        byte = raw[i]
        i += 1
        if byte in _WHITESPACE:
            pending_space = True
            continue
        if pending_space:
            if len(out) + 1 >= limit:
                break
            out.append(ord("_"))
            pending_space = False
        out.append(byte)
    return bytes(out)


def _utf8_length(data: bytes, start: int) -> int:
    """Length of the valid multi-byte UTF-8 character at ``start``, else 0."""
    for length in (2, 3, 4):
        try:
            data[start:start + length].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if len(data[start:start + length]) == length:
            return length
    return 0


def _replace_chars(raw: bytes) -> bytes:
    """udev_replace_chars with no extra allowed characters.

    Keeps ASCII letters and digits, ``#+-.:=@_``, a ``\\x`` escape and valid
    multi-byte UTF-8; every other byte becomes ``_``.
    """
    out = bytearray(raw)
    i = 0
    while i < len(out):
        byte = out[i]
        if (0x30 <= byte <= 0x39 or 0x41 <= byte <= 0x5A or 0x61 <= byte <= 0x7A
                or byte in _ALLOWED):
            i += 1
            continue
        if byte == ord("\\") and i + 1 < len(out) and out[i + 1] == ord("x"):
            i += 2
            continue
        length = _utf8_length(bytes(out), i) if byte >= 0x80 else 0
        if length > 1:
            i += length
            continue
        out[i] = ord("_")
        i += 1
    return bytes(out)


def _clean(text: str, limit: int = _STRING_LIMIT) -> str:
    raw = text.encode("utf-8", errors="surrogateescape")
    return _replace_chars(_replace_whitespace(raw, limit)).decode("utf-8", errors="replace")


def _usable_serial(serial: Optional[str]) -> Optional[str]:
    """The serial udev uses, or None: any byte outside 0x20-0x7f, or a comma,
    and udev ignores the serial altogether."""
    if not serial:
        return None
    raw = serial.encode("utf-8", errors="surrogateescape")
    if any(byte < 0x20 or byte > 0x7F or byte == ord(",") for byte in raw):
        return None
    cleaned = _replace_chars(_replace_whitespace(raw, len(raw)))
    return cleaned.decode("ascii") or None


def id_serial(vid: str, pid: str, manufacturer: Optional[str] = None,
              product: Optional[str] = None, serial: Optional[str] = None) -> str:
    """udev's ``ID_SERIAL``: ``<vendor>_<model>[_<serial>]``.

    The vendor and model are the device's manufacturer and product strings,
    or its hexadecimal vendor and product IDs when it has none.
    """
    vendor = _clean(manufacturer if manufacturer else vid.lower())
    model = _clean(product if product else pid.lower())
    name = f"{vendor}_{model}"
    serial_part = _usable_serial(serial)
    return f"{name}_{serial_part}" if serial_part else name


def by_id_name(vid, pid, manufacturer, product, serial, interface: int,
               usb_serial_adapter: bool) -> str:
    """The ``/dev/serial/by-id/`` entry name for one port."""
    name = f"usb-{id_serial(vid, pid, manufacturer, product, serial)}-if{interface:02x}"
    return name + "-port0" if usb_serial_adapter else name


def by_path_name(sysfs_name: str, interface: int, usb_serial_adapter: bool) -> str:
    """This Mac's ``/dev/serial/by-path/`` entry name for one port.

    ``sysfs_name`` is the device's Linux-style name (``1-1.2``).
    """
    bus, _, ports = sysfs_name.partition("-")
    name = f"{MAC_PATH_PREFIX}{bus}-usb-0:{ports}:1.{interface}"
    return name + "-port0" if usb_serial_adapter else name


def links(devices: Iterable[Dict], ports: Iterable[Dict], names: Dict[str, str],
          sysfs_name) -> Dict[str, str]:
    """``{link path: Linux port path}`` for every named port.

    ``devices`` is ``darwin_usb.parse_devices``'s list, ``ports``
    ``darwin_serial.parse_ports``'s, ``names`` maps each port's Mac device to
    its Linux name, and ``sysfs_name`` turns a location ID into the Linux-style
    device name. Where two ports would get the same by-id name (identical
    adapters without a serial), only one link can exist, as on Linux; here it
    is the lowest-numbered port's.
    """
    by_location = {}
    for device in devices:
        try:
            by_location[int(device["sysfs_name"], 16)] = device
        except (KeyError, TypeError, ValueError):
            continue
    table: Dict[str, str] = {}
    for port in sorted(ports, key=lambda p: _port_order(names.get(p.get("device")))):
        linux = names.get(port.get("device"))
        device = by_location.get(port.get("location"))
        if not linux or device is None or not device.get("vid") or not device.get("pid"):
            continue
        name = sysfs_name(port["location"])
        if name is None:
            continue
        interface = port.get("interface") or 0
        adapter = linux.startswith("ttyUSB")
        path = "/dev/" + linux
        for link in (BY_ID + by_id_name(device["vid"], device["pid"], device.get("manufacturer"),
                                        device.get("product"), device.get("serial"),
                                        interface, adapter),
                     BY_PATH + by_path_name(name, interface, adapter)):
            table.setdefault(link, path)
    return table


def _port_order(linux: Optional[str]):
    """ttyACM before ttyUSB, then by number: the order of the lowest name."""
    if not linux:
        return ("~", 0)
    kind = linux.rstrip("0123456789")
    return (kind, int(linux[len(kind):] or 0))


def is_mac_by_path(path: str) -> bool:
    return path.startswith(BY_PATH + MAC_PATH_PREFIX)


def not_here_message(path: str) -> Optional[str]:
    """Why a by-path name from another machine cannot be opened here, or None."""
    if path.startswith(BY_PATH) and not is_mac_by_path(path):
        return (f"{path} names a USB socket on the machine the net was saved on; "
                f"by-path names do not carry over to this Mac. Use the port's "
                f"/dev/serial/by-id name or a serial:// address, or this Mac's "
                f"own by-path name")
    return None
