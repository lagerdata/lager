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

The UART net path additionally uses the interface-aware pair below
(``identity_for_tty`` / ``resolve_identity``): a snapshot taken from a live tty
records vid/pid, USB serial, physical port path, and USB interface number, and
resolves back to whichever tty the same device owns after a re-enumeration —
including the correct channel of multi-interface chips (FT4232H) and
serial-less adapters (pinned to their physical port, same trade-off as the
``/port/`` address form above).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

_SYS_TTY = Path("/sys/class/tty")
_DEV = Path("/dev")
# udev's durable names: by-id/<vendor>_<product>_<serial>-ifNN[-portN] and
# by-path/<bus topology>[-portN]. Read as plain symlinks — the container has
# /dev bind-mounted but not necessarily /run/udev.
_DEV_SERIAL = Path("/dev/serial")
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


def _read_sysfs_display(path: Path) -> Optional[str]:
    """A device-reported sysfs string (manufacturer, product) for display.

    These strings come from the device's own descriptors, so they are
    untrusted: non-printable characters are dropped and an empty result is
    None. Use them only as display text — never in a path or a command.
    """
    text = _read_sysfs_text(path)
    if text is None:
        return None
    text = "".join(ch for ch in text if ch.isprintable()).strip()
    return text or None


def _serial_links(kind: str) -> Dict[str, str]:
    """Map tty name -> its ``/dev/serial/<kind>/`` symlink, in one pass.

    *kind* is ``by-id`` or ``by-path``. Each link is resolved and kept only
    if it lands directly in ``/dev``. udev makes both a ``-usb-`` and a
    ``-usbv2-`` by-path name for one tty; the ``-usb-`` one wins. A missing
    or unreadable directory yields an empty map — this never raises.
    """
    links: Dict[str, str] = {}
    try:
        dev_dir = os.path.realpath(str(_DEV))
        entries = sorted((_DEV_SERIAL / kind).iterdir(), key=lambda p: p.name)
        for link in entries:
            target = os.path.realpath(str(link))
            if os.path.dirname(target) != dev_dir:
                continue
            tty = os.path.basename(target)
            current = links.get(tty)
            if current is None or ("-usbv2-" in os.path.basename(current)
                                   and "-usbv2-" not in link.name):
                links[tty] = str(link)
    except OSError:
        return {}
    return links


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


# USB interface suffix of a sysfs dir name, e.g. "1-1.2:1.3" -> interface 3.
_IFACE_SUFFIX_RE = re.compile(r":\d+\.(\d+)$")


def _interface_for_tty(tty_dev: Path) -> Optional[int]:
    """USB interface number a tty belongs to, or None if undeterminable.

    Walks up from the tty's ``device`` link looking for the deepest ancestor
    whose name carries the ``:<config>.<interface>`` suffix — the same
    convention ``usb_scanner._walk_ttys`` harvests. Distinguishes the four
    channels of a multi-interface chip (FT4232H) that share one USB device.
    """
    device_link = tty_dev / "device"
    if not device_link.exists():
        return None
    try:
        node = device_link.resolve()
    except OSError:
        return None
    for _ in range(10):
        m = _IFACE_SUFFIX_RE.search(node.name)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        parent = node.parent
        if not parent or parent == node or parent == Path("/sys"):
            return None
        node = parent
    return None


def _serial_is_unique(vid: str, pid: str, serial: str, own_dir_name: str) -> bool:
    """False when another live USB device shares vid/pid/serial.

    Clone adapters (e.g. CP210x units all programmed with serial "0001") make
    the serial worthless as identity. Sibling interfaces of one multi-port
    chip share a device dir and are not counted as duplicates.
    """
    seen = set()
    try:
        for tty_dev in _SYS_TTY.iterdir():
            if not tty_dev.name.startswith(("ttyUSB", "ttyACM")):
                continue
            usb_dir = _usb_device_dir_for_tty(tty_dev)
            if usb_dir is None or usb_dir.name == own_dir_name or usb_dir.name in seen:
                continue
            seen.add(usb_dir.name)
            if ((_read_sysfs_text(usb_dir / "idVendor") or "").lower() == vid
                    and (_read_sysfs_text(usb_dir / "idProduct") or "").lower() == pid
                    and _read_sysfs_text(usb_dir / "serial") == serial):
                return False
    except OSError:
        # Unreadable sysfs mid-walk: assume unique (degrades to trusting the
        # serial, same as the other walkers' missing-_SYS_TTY guards).
        pass
    return True


def identity_for_tty(tty: str) -> Optional[Dict[str, Optional[str]]]:
    """Snapshot the durable USB identity of a live tty node.

    Accepts ``/dev/ttyUSB*`` / ``/dev/ttyACM*`` or a symlink to one (e.g. a
    ``/dev/serial/by-id/...`` path). Returns ``{"vid", "pid", "serial",
    "port_path", "interface"}`` or None when the tty is not backed by a USB
    device (or is gone).

    A serial shared with another live device (clone serials) is recorded as
    None: it cannot identify the device, so the snapshot pins to the physical
    port instead. Otherwise a later resolution while this device is briefly
    off the bus could match a look-alike sibling.
    """
    if not tty or not isinstance(tty, str):
        return None
    try:
        name = os.path.basename(os.path.realpath(tty))
    except OSError:
        return None
    # basename() above already strips every directory component; this states
    # where the result is allowed to land, next to the join that produces it.
    # The helpers below walk outward from here into /sys/devices by design, so
    # the containment belongs at this entry point rather than in each of them.
    tty_dev = Path(os.path.normpath(str(_SYS_TTY / name)))
    if not str(tty_dev).startswith(str(_SYS_TTY) + os.sep):
        return None
    usb_dir = _usb_device_dir_for_tty(tty_dev)
    if usb_dir is None:
        return None
    vid = (_read_sysfs_text(usb_dir / "idVendor") or "").lower()
    pid = (_read_sysfs_text(usb_dir / "idProduct") or "").lower()
    if not vid or not pid:
        return None
    serial = _read_sysfs_text(usb_dir / "serial")
    if serial and not _serial_is_unique(vid, pid, serial, usb_dir.name):
        serial = None
    return {
        "vid": vid,
        "pid": pid,
        "serial": serial,
        "port_path": usb_dir.name,
        "interface": _interface_for_tty(tty_dev),
    }


def resolve_identity(ident) -> Optional[str]:
    """Resolve an ``identity_for_tty`` snapshot back to the live tty node.

    Match rules: vid/pid always; interface when both sides know it; then USB
    serial when the snapshot has one. A serial that several live devices
    share (clone adapters) proves nothing, so the physical port is then
    REQUIRED — with the true device absent this returns None so callers keep
    retrying instead of grabbing a look-alike sibling. Serial-less snapshots
    resolve by physical port path. Returns ``/dev/tty...`` or None if the
    device is not (yet) back. Tolerates arbitrary garbage input — a malformed
    snapshot resolves to None rather than raising.
    """
    if not isinstance(ident, dict):
        return None
    vid = ident.get("vid")
    pid = ident.get("pid")
    if not isinstance(vid, str) or not isinstance(pid, str):
        return None
    vid = vid.lower()
    pid = pid.lower()
    if not vid or not pid:
        return None
    serial = ident.get("serial")
    port_path = ident.get("port_path")
    want_iface = ident.get("interface")
    if not _SYS_TTY.exists():
        return None

    candidates = []  # (tty name, usb device-dir basename, usb serial)
    for tty_dev in sorted(_SYS_TTY.iterdir(), key=lambda p: p.name):
        if not tty_dev.name.startswith(("ttyUSB", "ttyACM")):
            continue
        usb_dir = _usb_device_dir_for_tty(tty_dev)
        if usb_dir is None:
            continue
        if (_read_sysfs_text(usb_dir / "idVendor") or "").lower() != vid:
            continue
        if (_read_sysfs_text(usb_dir / "idProduct") or "").lower() != pid:
            continue
        if want_iface is not None:
            iface = _interface_for_tty(tty_dev)
            if iface is not None and iface != want_iface:
                continue
        candidates.append(
            (tty_dev.name, usb_dir.name, _read_sysfs_text(usb_dir / "serial"))
        )

    if serial:
        matches = [c for c in candidates if c[2] == serial]
        if not matches:
            return None
        if len(matches) > 1:
            # Several live devices share this serial (clone serials): the
            # serial proves nothing, so the physical port is REQUIRED. When
            # our port's device is absent, fail — the caller keeps retrying —
            # rather than grabbing a look-alike sibling mid-re-enumeration.
            if not port_path:
                return None
            for c in matches:
                if c[1] == port_path:
                    return f"/dev/{c[0]}"
            return None
        return f"/dev/{matches[0][0]}"
    if port_path:
        for c in candidates:
            if c[1] == port_path:
                return f"/dev/{c[0]}"
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


def list_cables() -> List[Dict[str, Any]]:
    """Enumerate live USB-serial cables, one record per tty.

    Returns one record per ``/dev/ttyUSB*`` / ``/dev/ttyACM*`` backed by a
    USB device, with these keys:

    - ``vid``, ``pid``: lowercase hex IDs.
    - ``serial``: the USB serial, or None for cables without a programmed one.
    - ``port_path``: the sysfs device-dir basename (e.g. ``1-1.2``) used by
      the port-pinned assignment form.
    - ``tty``: the live ``/dev/tty*`` node. It can renumber on replug.
    - ``manufacturer``, ``product``: the device's own descriptor strings,
      or None. They are device-controlled, so treat them as display text only.
    - ``interface``: the USB interface number (int), which tells apart the
      ports of a multi-port chip such as an FT4232H, or None.
    - ``by_id``: the ``/dev/serial/by-id/...`` link for this tty, or None.
      Two identical serial-less adapters share one by-id name and udev keeps
      only one link, so only the tty it resolves to gets it.
    - ``by_path``: the ``/dev/serial/by-path/...`` link (the ``-usb-``
      variant, not ``-usbv2-``), or None. ACM links have no ``-port0`` suffix.

    When ``serial`` is None, consumers should prefer ``by_path`` over
    ``by_id`` as the durable path.

    The first five keys are the cable picker's data source: the assign flow
    identifies a cable by serial or port path and captures its vid/pid from
    here, so an assignment can only be created for hardware that is actually
    plugged in.
    """
    cables: List[Dict[str, Any]] = []
    if not _SYS_TTY.exists():
        return cables
    by_id = _serial_links("by-id")
    by_path = _serial_links("by-path")
    for tty_dev in sorted(_SYS_TTY.iterdir(), key=lambda p: p.name):
        if not tty_dev.name.startswith(("ttyUSB", "ttyACM")):
            continue
        usb_dir = _usb_device_dir_for_tty(tty_dev)
        if usb_dir is None:
            continue
        vid = (_read_sysfs_text(usb_dir / "idVendor") or "").lower()
        pid = (_read_sysfs_text(usb_dir / "idProduct") or "").lower()
        if not vid or not pid:
            continue
        cables.append({
            "vid": vid,
            "pid": pid,
            "serial": _read_sysfs_text(usb_dir / "serial"),
            "port_path": usb_dir.name,
            "tty": f"/dev/{tty_dev.name}",
            "manufacturer": _read_sysfs_display(usb_dir / "manufacturer"),
            "product": _read_sysfs_display(usb_dir / "product"),
            "interface": _interface_for_tty(tty_dev),
            "by_id": by_id.get(tty_dev.name),
            "by_path": by_path.get(tty_dev.name),
        })
    return cables
