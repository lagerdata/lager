# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Cross-platform USB and serial device enumeration.

The Lager box originally enumerated USB devices by reading /sys/bus/usb/devices
directly, which is Linux-only. On a native macOS box this module dispatches
to pyusb (libusb backend) and pyserial.tools.list_ports instead.

All functions return data in a single canonical shape so callers don't need
platform branches. The Linux paths preserve the original sysfs behaviour to
avoid disturbing existing boxes.

Public API:
    iter_usb_devices()      -> list of {"vid", "pid", "serial"}
    get_tty_for_usb_serial(serial) -> Optional[str]  (e.g. /dev/ttyUSB0 or /dev/cu.usbserial-X)
    get_serial_by_port(path)       -> Optional[str]
    iter_serial_ports()     -> list of (device_path, vid, pid, serial)
    iter_video_devices()    -> list of strings (paths or indices)
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_IS_DARWIN = sys.platform == "darwin"
_IS_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
#  USB device enumeration
# ---------------------------------------------------------------------------

def iter_usb_devices() -> List[dict]:
    """Return every USB device on the host as {'vid','pid','serial'} dicts.

    vid and pid are lowercase 4-character hex strings (no '0x' prefix).
    serial may be None when the device has no serial descriptor.
    """
    if _IS_DARWIN:
        return _iter_usb_darwin()
    return _iter_usb_linux()


def _iter_usb_linux() -> List[dict]:
    results: List[dict] = []
    sys_usb = Path("/sys/bus/usb/devices")
    if not sys_usb.exists():
        return results
    for dev in sys_usb.iterdir():
        try:
            vid_path = dev / "idVendor"
            pid_path = dev / "idProduct"
            serial_path = dev / "serial"
            if not (vid_path.exists() and pid_path.exists()):
                continue
            vid_fd = os.open(str(vid_path), os.O_RDONLY | os.O_NONBLOCK)
            vid = os.read(vid_fd, 64).decode("utf-8").strip().lower()
            os.close(vid_fd)
            pid_fd = os.open(str(pid_path), os.O_RDONLY | os.O_NONBLOCK)
            pid = os.read(pid_fd, 64).decode("utf-8").strip().lower()
            os.close(pid_fd)
            serial: Optional[str] = None
            if serial_path.exists():
                try:
                    serial_fd = os.open(str(serial_path), os.O_RDONLY | os.O_NONBLOCK)
                    serial = os.read(serial_fd, 256).decode("utf-8").strip()
                    os.close(serial_fd)
                except (OSError, UnicodeDecodeError):
                    serial = None
        except (OSError, UnicodeDecodeError, BlockingIOError):
            continue
        results.append({"vid": vid, "pid": pid, "serial": serial})
    return results


def _iter_usb_darwin() -> List[dict]:
    try:
        import usb.core
    except ImportError:
        return []
    results: List[dict] = []
    try:
        devices = list(usb.core.find(find_all=True))
    except Exception:
        return results
    for dev in devices:
        try:
            vid = f"{dev.idVendor:04x}"
            pid = f"{dev.idProduct:04x}"
        except Exception:
            continue
        serial: Optional[str] = None
        try:
            if dev.iSerialNumber:
                serial = usb.util.get_string(dev, dev.iSerialNumber)
                if serial is not None:
                    serial = serial.strip() or None
        except Exception:
            serial = None
        results.append({"vid": vid, "pid": pid, "serial": serial})
    return results


# ---------------------------------------------------------------------------
#  Serial port → tty path resolution by USB serial number
# ---------------------------------------------------------------------------

def get_tty_for_usb_serial(serial_number: Optional[str]) -> Optional[str]:
    """Return the /dev/tty* (or /dev/cu.*) path for a USB device serial."""
    if not serial_number:
        return None
    if _IS_DARWIN:
        return _tty_for_serial_darwin(serial_number)
    return _tty_for_serial_linux(serial_number)


def _tty_for_serial_linux(serial_number: str) -> Optional[str]:
    sys_tty = Path("/sys/class/tty")
    if not sys_tty.exists():
        return None
    for tty_dev in sys_tty.iterdir():
        try:
            if not tty_dev.name.startswith(("ttyUSB", "ttyACM")):
                continue
            device_path = tty_dev / "device"
            if not device_path.exists():
                continue
            usb_device = device_path.resolve()
            for _ in range(10):
                serial_path = usb_device / "serial"
                if serial_path.exists():
                    try:
                        fd = os.open(str(serial_path), os.O_RDONLY | os.O_NONBLOCK)
                        dev_serial = os.read(fd, 256).decode("utf-8").strip()
                        os.close(fd)
                        if dev_serial == serial_number:
                            return f"/dev/{tty_dev.name}"
                        break
                    except (OSError, UnicodeDecodeError, BlockingIOError):
                        break
                usb_device = usb_device.parent
                if not usb_device or usb_device == Path("/sys"):
                    break
        except Exception:
            continue
    return None


def _tty_for_serial_darwin(serial_number: str) -> Optional[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    try:
        for port in list_ports.comports():
            if port.serial_number and port.serial_number == serial_number:
                return port.device
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
#  Reverse: serial port path → USB serial number
# ---------------------------------------------------------------------------

def get_serial_by_port(port_path: str) -> Optional[str]:
    """Return the USB serial number for a tty/cu device path."""
    if _IS_DARWIN:
        return _serial_by_port_darwin(port_path)
    return _serial_by_port_linux(port_path)


def _serial_by_port_linux(port_path: str) -> Optional[str]:
    try:
        output = subprocess.check_output(
            ["udevadm", "info", "-q", "all", "-n", port_path],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        for key in ("ID_SERIAL_SHORT", "ID_SERIAL", "ID_USB_SERIAL"):
            match = re.search(fr"{key}=(\w+)", output)
            if match:
                return match.group(1)
        output = subprocess.check_output(
            ["udevadm", "info", "-a", "-n", port_path],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        match = re.search(r'ATTRS\{serial\}=="([^"]+)"', output)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def _serial_by_port_darwin(port_path: str) -> Optional[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    try:
        for port in list_ports.comports():
            if port.device == port_path and port.serial_number:
                return port.serial_number
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
#  Serial port enumeration (for handshake-based detection like Dexarm)
# ---------------------------------------------------------------------------

def iter_serial_ports() -> List[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
    """Return every serial-capable device as (path, vid, pid, serial) tuples.

    vid/pid/serial may be None on Linux (where this falls back to a tty glob)
    but are typically populated on macOS via pyserial's list_ports.
    """
    if _IS_DARWIN:
        return _iter_serial_ports_darwin()
    return _iter_serial_ports_linux()


def _iter_serial_ports_linux() -> List[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
    paths = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    return [(p, None, None, None) for p in paths]


def _iter_serial_ports_darwin() -> List[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out: List[Tuple[str, Optional[str], Optional[str], Optional[str]]] = []
    try:
        for port in list_ports.comports():
            # Prefer /dev/cu.* over /dev/tty.* on macOS for non-blocking opens
            if port.device.startswith("/dev/tty.") or port.device.startswith("/dev/cu."):
                vid = f"{port.vid:04x}" if port.vid is not None else None
                pid = f"{port.pid:04x}" if port.pid is not None else None
                out.append((port.device, vid, pid, port.serial_number))
    except Exception:
        return []
    return out


# ---------------------------------------------------------------------------
#  Video device enumeration (webcams)
# ---------------------------------------------------------------------------

def iter_video_devices() -> List[str]:
    """Return a sorted list of video device identifiers.

    On Linux this is the /dev/video* glob; on macOS this returns OpenCV-style
    integer indices as strings (the macOS AVFoundation backend does not expose
    /dev paths). Callers that pass these to cv2.VideoCapture need to handle
    both string paths and integer indices.
    """
    if _IS_DARWIN:
        return _iter_video_devices_darwin()
    return _iter_video_devices_linux()


def _iter_video_devices_linux() -> List[str]:
    nodes = sorted(
        Path(p) for p in glob.glob("/dev/video*")
    )
    nodes.sort(key=lambda p: int(re.sub(r"\D", "", p.name) or 0))
    return [str(p) for p in nodes]


def _iter_video_devices_darwin() -> List[str]:
    """Count cameras on macOS without triggering Camera permission prompts.

    Uses `system_profiler SPCameraDataType`, which reads from IOKit and does
    not require Camera entitlement. Returns OpenCV-style integer indices as
    strings (0, 1, 2, ...) — the order matches AVFoundation's enumeration,
    which is the same order cv2.VideoCapture(idx) uses.
    """
    try:
        output = subprocess.check_output(
            ["system_profiler", "SPCameraDataType"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    # Each connected camera produces a "Model ID:" line in the output.
    count = sum(1 for line in output.splitlines() if "Model ID:" in line)
    return [str(i) for i in range(count)]
