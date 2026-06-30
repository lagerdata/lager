# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""USB instrument scanner for the Lager Box HTTP server.

Extracted from cli/impl/query_instruments.py so the scan logic can be
imported by HTTP handlers that are deployed inside the box container.
The CLI version (query_instruments.py) remains the canonical copy for
CLI usage; this module keeps the box HTTP server self-contained.
"""

import glob
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, TypeVar

try:
    # Custom-device framework (manual cable-to-instrument assignments). Guarded
    # so a partial deployment without lager.devices degrades to "no custom
    # devices" instead of taking the scanner down — same philosophy as the
    # box_http_server route-import guards.
    from lager.devices import catalog as _catalog
    from lager.devices import custom_store as _custom_store
    from lager.devices import serial_id as _serial_id
except Exception:
    _catalog = _custom_store = _serial_id = None

T = TypeVar('T')


# ---------------------------------------------------------------------------
#  Timeout helper
# ---------------------------------------------------------------------------

class _ScanTimeoutError(Exception):
    pass


def _timeout_handler(signum, frame):
    raise _ScanTimeoutError("Operation timed out")


def with_timeout(seconds: int, default: T = None) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator that aborts a function after *seconds* and returns *default*."""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args, **kwargs) -> T:
            # signal.signal() only works on the main thread. When this runs
            # inside a ThreadingHTTPServer worker (e.g. /instruments/list),
            # fall back to running without a signal-based timeout.
            if threading.current_thread() is not threading.main_thread():
                return func(*args, **kwargs)
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(seconds)
            try:
                result = func(*args, **kwargs)
                signal.alarm(0)
                return result
            except _ScanTimeoutError:
                return default
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
#  Static USB table (mirrors SUPPORTED_USB in query_instruments.py)
# ---------------------------------------------------------------------------

SUPPORTED_USB: Dict[str, Dict] = {
    # supply
    "Rigol_DP811":       {"vid": "1ab1", "pid": "0e11", "net_type": ["power-supply"]},
    "Rigol_DP821":       {"vid": "1ab1", "pid": "0e11", "net_type": ["power-supply"]},
    "Rigol_DP831":       {"vid": "1ab1", "pid": "0e31", "net_type": ["power-supply"]},
    "Rigol_DP832":       {"vid": "1ab1", "pid": "0e11", "net_type": ["power-supply"]},
    "EA_PSB_10080_60":   {"vid": "232e", "pid": "0053", "net_type": ["power-supply", "solar"]},
    "EA_PSB_10060_60":   {"vid": "232e", "pid": "0053", "net_type": ["power-supply", "solar"]},
    "KEYSIGHT_E36233A":  {"vid": "2a8d", "pid": "3302", "net_type": ["power-supply"]},
    "KEYSIGHT_E36313A":  {"vid": "2a8d", "pid": "1202", "net_type": ["power-supply"]},
    "KEYSIGHT_E36312A":  {"vid": "2a8d", "pid": "1102", "net_type": ["power-supply"]},
    # battery
    "Keithley_2281S":    {"vid": "05e6", "pid": "2281", "net_type": ["battery", "power-supply"]},
    # scope
    "Rigol_MS05204":     {"vid": "1ab1", "pid": "0515", "net_type": ["scope"]},
    "Picoscope_2000":    {"vid": "0ce9", "pid": "1007", "net_type": ["scope"]},
    # adc / gpio / dac / spi / i2c
    "LabJack_T7":        {"vid": "0cd5", "pid": "0007", "net_type": ["gpio", "adc", "dac", "spi", "i2c"]},
    "Aardvark":          {"vid": "0403", "pid": "e0d0", "net_type": ["spi", "i2c", "gpio"]},
    # FT232H — single channel. The chip can run in MPSSE mode (SPI / I2C /
    # GPIO / JTAG-SWD via libftdi) OR in async-serial mode (UART via
    # ``ftdi_sio`` → ``/dev/ttyUSB0``), but not both simultaneously. We
    # advertise every role so a single FT232H can host any one of them; the
    # user is responsible for not mixing MPSSE and UART roles on the same
    # chip.
    "FTDI_FT232H":       {"vid": "0403", "pid": "6014", "net_type": ["spi", "i2c", "gpio", "debug", "uart"]},
    # FT2232H — two MPSSE channels (A and B). The most common FTDI variant
    # used for ARM debugging (Olimex, generic JTAG cables); channel A is
    # typically wired to JTAG/SWD and channel B is free for SPI/I2C/GPIO or
    # UART. We advertise every role here so a single chip can host one debug
    # net (channel A) plus a UART net (channel B) at the same time.
    "FTDI_FT2232H":      {"vid": "0403", "pid": "6010", "net_type": ["spi", "i2c", "gpio", "debug", "uart"]},
    "MCC_USB-202":       {"vid": "09db", "pid": "012b", "net_type": ["adc", "dac", "gpio"]},
    # debug — J-Link family (handled by the J-Link backend)
    "J-Link":            {"vid": "1366", "pid": "1024", "net_type": ["debug"]},
    "J-Link_Plus":       {"vid": "1366", "pid": "0101", "net_type": ["debug"]},
    "J-Link_Base_Compact": {"vid": "1366", "pid": "1020", "net_type": ["debug"]},
    "Flasher_ARM":       {"vid": "1366", "pid": "0503", "net_type": ["debug"]},
    "J-Link_Flasher_Pro": {"vid": "1366", "pid": "0105", "net_type": ["debug"]},
    # debug — OpenOCD-backed probes (ST-Link, CMSIS-DAP, FTDI)
    # ST-Link v2 / v2-1 / v3 share a single OpenOCD interface/stlink.cfg.
    "STLink_v2":         {"vid": "0483", "pid": "3748", "net_type": ["debug"]},
    "STLink_v2_1":       {"vid": "0483", "pid": "374b", "net_type": ["debug"]},
    "STLink_v3_Mini":    {"vid": "0483", "pid": "374d", "net_type": ["debug"]},
    "STLink_v3":         {"vid": "0483", "pid": "374e", "net_type": ["debug"]},
    "STLink_v3_2VCP":    {"vid": "0483", "pid": "374f", "net_type": ["debug"]},
    # Raspberry Pi Picoprobe (CMSIS-DAP firmware on an RP2040).
    "RP2040_Picoprobe":  {"vid": "2e8a", "pid": "000c", "net_type": ["debug"]},
    # Atmel EDBG (CMSIS-DAP) — common on SAMD dev boards.
    "Atmel_EDBG":        {"vid": "03eb", "pid": "2111", "net_type": ["debug"]},
    # NXP / ARM DAPLink-style CMSIS-DAP adapters.
    "DAPLink":           {"vid": "0d28", "pid": "0204", "net_type": ["debug"]},
    # FTDI-based debug adapters (Olimex ARM-USB-OCD-H etc.) share PIDs with
    # the general-purpose FTDI chips already in this table, so we **add**
    # the debug net_type to the existing FTDI entries below rather than
    # duplicating them.
    # usb
    "Acroname_8Port":    {"vid": "24ff", "pid": "0013", "net_type": ["usb"]},
    "Acroname_4Port":    {"vid": "24ff", "pid": "0011", "net_type": ["usb"]},
    "YKUSH_Hub":         {"vid": "04d8", "pid": "f2f7", "net_type": ["usb"]},
    # eload
    "Rigol_DL3021":      {"vid": "1ab1", "pid": "0e11", "net_type": ["eload"]},
    # camera
    "Logitech_BRIO_HD":  {"vid": "046d", "pid": "085e", "net_type": ["webcam"]},
    "Logitech_BRIO":     {"vid": "046d", "pid": "0856", "net_type": ["webcam"]},
    "Logitech_C930e":    {"vid": "046d", "pid": "0843", "net_type": ["webcam"]},
    # watt-meter
    "Yocto_Watt":        {"vid": "24e0", "pid": "002a", "net_type": ["watt-meter"]},
    "Joulescope_JS220":  {"vid": "16d0", "pid": "10ba", "net_type": ["watt-meter", "energy-analyzer"]},
    "Nordic_PPK2":       {"vid": "1915", "pid": "c00a", "net_type": ["watt-meter", "energy-analyzer"]},
    # thermocouple
    "Phidget":           {"vid": "06c2", "pid": "0046", "net_type": ["thermocouple"]},
    # uart
    "Prolific_USB_Serial": {"vid": "067b", "pid": "23a3", "net_type": ["uart"]},
    "SiLabs_CP210x":     {"vid": "10c4", "pid": "ea60", "net_type": ["uart"]},
    "FTDI_FT232R":       {"vid": "0403", "pid": "6001", "net_type": ["uart"]},
    # FT4232H — four channels (A/B/C/D). A and B can run MPSSE (JTAG/SWD),
    # so we advertise ``debug`` alongside ``uart``. C and D are UART-only.
    # The OpenOCD backend reads the FTDI channel index out of the debug net's
    # ``device`` field (``STM32F4x@A``) or its ``probe_channel`` field.
    "FTDI_FT4232H":      {"vid": "0403", "pid": "6011", "net_type": ["uart", "debug"]},
    "ESP32_JTAG_Serial": {"vid": "303a", "pid": "1001", "net_type": ["uart"]},
}

CHANNEL_MAPS: Dict[str, Dict[str, List[str]]] = {
    "Rigol_DP811":            {"power-supply": ["1"]},
    "Rigol_DP821":            {"power-supply": ["1", "2"]},
    "Rigol_DP831":            {"power-supply": ["1", "2", "3"]},
    "Rigol_DP832":            {"power-supply": ["1", "2", "3"]},
    "EA_PSB_10080_60":        {"power-supply": ["1"], "solar": ["1"]},
    "EA_PSB_10060_60":        {"power-supply": ["1"], "solar": ["1"]},
    "KEYSIGHT_E36233A":       {"power-supply": ["1", "2"]},
    "KEYSIGHT_E36313A":       {"power-supply": ["1", "2", "3"]},
    "KEYSIGHT_E36312A":       {"power-supply": ["1", "2", "3"]},
    "Keithley_2281S":         {"power-supply": ["1"], "battery": ["1"]},
    "Picoscope_2000":         {"scope": ["1", "2"]},
    "Rigol_MS05204":          {"scope": ["1", "2", "3", "4"], "logic": ["1"]},
    "LabJack_T7": {
        "gpio": [
            "CIO0", "CIO1", "CIO2", "CIO3",
            "EIO0", "EIO1", "EIO2", "EIO3", "EIO4", "EIO5", "EIO6", "EIO7",
            "MIO0", "MIO1", "MIO2",
            "FIO0", "FIO1", "FIO2", "FIO3", "FIO4", "FIO5", "FIO6", "FIO7",
        ],
        "adc": [
            "AIN0", "AIN1", "AIN2", "AIN3", "AIN4", "AIN5", "AIN6", "AIN7",
            "AIN8", "AIN9", "AIN10", "AIN11", "AIN12", "AIN13",
        ],
        "dac": ["DAC0", "DAC1"],
        "spi": ["FIO0-FIO3"],
        "i2c": ["FIO4-FIO5"],
    },
    "MCC_USB-202": {
        "adc": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5", "CH6", "CH7"],
        "dac": ["DAC0", "DAC1"],
        "gpio": ["DIO0", "DIO1", "DIO2", "DIO3", "DIO4", "DIO5", "DIO6", "DIO7"],
    },
    "Aardvark": {
        "spi": ["SPI0"],
        "i2c": ["I2C0"],
        "gpio": ["SCL", "SDA", "MISO", "SCK", "MOSI", "SS"],
    },
    "FTDI_FT232H": {
        "spi": ["SPI0"],
        "i2c": ["I2C0"],
        "gpio": ["4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"],
        "debug": ["DEVICE_TYPE"],
        # UART channels are filled in at scan time from the actual tty
        # device path (``/dev/ttyUSB<N>``); the scanner replaces this list
        # when it enumerates the chip's tty. Empty default keeps the role
        # registered without claiming a specific path.
        "uart": [],
    },
    "FTDI_FT2232H": {
        "spi": ["SPI0"],
        "i2c": ["I2C0"],
        "gpio": ["4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"],
        # Multi-channel: DEVICE_TYPE@A and DEVICE_TYPE@B are both placeholders
        # that the CLI/TUI prompts replace with a real MCU name; the suffix
        # tells the OpenOCD backend which interface to bind to.
        "debug": ["DEVICE_TYPE@A", "DEVICE_TYPE@B"],
        # UART tty paths are populated at scan time (same as FT232H); empty
        # default avoids handing the TUI raw interface indices like ``0``
        # that would later land in the saved net's ``pin`` field and break
        # the UART dispatcher's serial lookup.
        "uart": [],
    },
    "FTDI_FT4232H": {
        # A and B can drive JTAG/SWD via OpenOCD's ftdi driver; C/D cannot.
        "debug": ["DEVICE_TYPE@A", "DEVICE_TYPE@B"],
        # See FT2232H note above — placeholder list is filled with real
        # ``/dev/ttyUSB*`` paths by the scanner; if enumeration fails the
        # role is dropped entirely rather than advertised as a bare index.
        "uart": [],
    },
    "J-Link":                 {"debug": ["DEVICE_TYPE"]},
    "J-Link_Plus":            {"debug": ["DEVICE_TYPE"]},
    "J-Link_Base_Compact":    {"debug": ["DEVICE_TYPE"]},
    "Flasher_ARM":            {"debug": ["DEVICE_TYPE"]},
    "J-Link_Flasher_Pro":     {"debug": ["DEVICE_TYPE"]},
    "STLink_v2":              {"debug": ["DEVICE_TYPE"]},
    "STLink_v2_1":            {"debug": ["DEVICE_TYPE"]},
    "STLink_v3_Mini":         {"debug": ["DEVICE_TYPE"]},
    "STLink_v3":              {"debug": ["DEVICE_TYPE"]},
    "STLink_v3_2VCP":         {"debug": ["DEVICE_TYPE"]},
    "RP2040_Picoprobe":       {"debug": ["DEVICE_TYPE"]},
    "Atmel_EDBG":             {"debug": ["DEVICE_TYPE"]},
    "DAPLink":                {"debug": ["DEVICE_TYPE"]},
    "Acroname_8Port":         {"usb": ["0", "1", "2", "3", "4", "5", "6", "7"]},
    "Acroname_4Port":         {"usb": ["0", "1", "2", "3"]},
    "YKUSH_Hub":              {"usb": ["1", "2", "3"]},
    "Rigol_DL3021":           {"eload": ["1"]},
    "Yocto_Watt":             {"watt-meter": ["0"]},
    "Joulescope_JS220":       {"watt-meter": ["0"], "energy-analyzer": ["0"]},
    "Nordic_PPK2":            {"watt-meter": ["0"], "energy-analyzer": ["0"]},
    "Phidget":                {"thermocouple": ["0", "1", "2", "3"]},
    "Prolific_USB_Serial":    {"uart": ["0"]},
    "SiLabs_CP210x":          {"uart": ["0"]},
    "FTDI_FT232R":            {"uart": ["0"]},
    "ESP32_JTAG_Serial":      {"uart": ["0"]},
}

# VID:PID → instrument name lookup (excludes instruments with duplicate PIDs)
_VIDPID_TO_NAME: Dict[tuple, str] = {}
for _name, _meta in SUPPORTED_USB.items():
    if _name in ("Rigol_DL3021", "Rigol_DP811", "Rigol_DP832"):
        continue  # differentiated by serial number, handled in _scan_usb
    _VIDPID_TO_NAME[(_meta["vid"].lower(), _meta["pid"].lower())] = _name


# ---------------------------------------------------------------------------
#  UART tty helper
# ---------------------------------------------------------------------------

@with_timeout(seconds=2, default=None)
def _get_tty_for_usb_serial(serial_number: Optional[str]) -> Optional[str]:
    """Return the FIRST tty bound to a USB device with *serial_number*.

    Kept for back-compat with single-channel callers. For multi-channel
    chips (FT2232H, FT4232H, ESP32 CDC-ACM dual) use
    :func:`_get_ttys_for_usb_serial` to enumerate all interfaces.
    """
    ttys = _get_ttys_for_usb_serial(serial_number) or []
    if not ttys:
        return None
    # Sort by interface number so the result is deterministic across kernel
    # enumeration order (interface 0 wins on multi-channel chips).
    ttys.sort(key=lambda t: (t.get("interface", 0), t.get("path", "")))
    return ttys[0]["path"]


# Parses interface numbers out of sysfs USB paths like ``3-1:1.2`` (interface
# 2 on USB device 3-1). The colon separates device address from config:iface.
_USB_INTERFACE_RE = re.compile(r":\d+\.(\d+)$")


@with_timeout(seconds=2, default=None)
def _get_ttys_for_usb_serial(serial_number: Optional[str]):
    """Return ``[{'path', 'interface'}]`` for every tty bound to *serial_number*.

    Multi-channel FTDIs (FT2232H, FT4232H) expose one USB interface per
    MPSSE/UART channel, each backed by its own ``/dev/ttyUSB<N>`` (or
    ``ttyACM`` for CDC firmware). We walk every tty node, traverse the
    parent chain to find the USB device whose ``serial`` matches, and
    capture the interface number from the immediate child path
    (``...:1.<iface>``). Returns ``[]`` (not ``None``) on no matches.
    """
    if not serial_number:
        return []
    return _walk_ttys(match=lambda usb_dir: _read_sysfs_text(usb_dir / "serial") == serial_number)


@with_timeout(seconds=2, default=None)
def _get_ttys_for_usb_device(usb_device_path: Optional[Path]):
    """Return ``[{'path', 'interface'}]`` for every tty under *usb_device_path*.

    Sibling of :func:`_get_ttys_for_usb_serial` for the case where the
    physical USB device has no programmed serial number — typical for
    bare FT2232H/FT4232H chips whose EEPROM was never burnt. We still
    want to expose their UART interfaces in the TUI, so we match against
    the device's sysfs node (e.g. ``/sys/bus/usb/devices/3-1``) instead
    of its ``serial`` file. Each tty's parent chain is walked until we
    hit the same resolved sysfs path.

    Ambiguity caveat: when multiple identical chips of the same VID:PID
    are plugged in without serials, the caller is responsible for
    iterating each ``usb_device_path`` separately — this function only
    reports ttys for the one device it was given.
    """
    if not usb_device_path:
        return []
    try:
        target = usb_device_path.resolve()
    except OSError:
        return []
    return _walk_ttys(match=lambda usb_dir: usb_dir == target)


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


def _walk_ttys(*, match):
    """Walk ``/sys/class/tty`` and collect ttys whose parent USB device
    satisfies *match(usb_dir)*.

    *match* is invoked at each level of the parent chain with the
    resolved sysfs directory. The first level where *match* returns
    truthy wins; if no level matches before we cross out of the USB
    hierarchy, the tty is skipped. The interface number is harvested
    from the deepest ``:1.<N>`` segment crossed on the way up, which is
    always the USB interface this tty was bound to.
    """
    sys_tty = Path("/sys/class/tty")
    if not sys_tty.exists():
        return []
    matches = []
    for tty_dev in sys_tty.iterdir():
        try:
            if not tty_dev.name.startswith(("ttyUSB", "ttyACM")):
                continue
            device_path = tty_dev / "device"
            if not device_path.exists():
                continue

            resolved = device_path.resolve()

            iface_num = 0
            iface_match = _USB_INTERFACE_RE.search(resolved.name)
            if iface_match:
                iface_num = int(iface_match.group(1))

            usb_device = resolved
            for _ in range(10):
                im = _USB_INTERFACE_RE.search(usb_device.name)
                if im:
                    iface_num = int(im.group(1))

                # The USB device directory is the one with idVendor; stop
                # walking once we see it, regardless of match result, so
                # we don't keep climbing into the host controller.
                try:
                    is_usb_device = (usb_device / "idVendor").exists()
                except OSError:
                    is_usb_device = False

                try:
                    hit = bool(match(usb_device))
                except Exception:  # noqa: BLE001 — match callbacks are user-supplied
                    hit = False

                if hit:
                    matches.append({
                        "path": f"/dev/{tty_dev.name}",
                        "interface": iface_num,
                    })
                    break

                if is_usb_device:
                    # Reached the device root and didn't match — give up
                    # on this tty rather than crossing into the hub.
                    break

                usb_device = usb_device.parent
                if not usb_device or usb_device == Path("/sys"):
                    break
        except Exception:
            continue
    return matches


# ---------------------------------------------------------------------------
#  Core USB sysfs scan
# ---------------------------------------------------------------------------

def scan_usb() -> List[dict]:
    """Quick VID:PID scan via /sys/bus/usb/devices (Linux only)."""
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

            if (vid, pid) not in _VIDPID_TO_NAME and not (vid == "1ab1" and pid == "0e11"):
                continue

            serial = None
            if serial_path.exists():
                try:
                    serial_fd = os.open(str(serial_path), os.O_RDONLY | os.O_NONBLOCK)
                    serial = os.read(serial_fd, 256).decode("utf-8").strip()
                    os.close(serial_fd)
                except (OSError, UnicodeDecodeError):
                    serial = None
        except (OSError, UnicodeDecodeError, BlockingIOError):
            continue

        # Differentiate Rigol instruments sharing VID:PID 1ab1:0e11
        if vid == "1ab1" and pid == "0e11" and serial:
            serial_upper = serial.upper()
            if serial_upper.startswith("DL3"):
                meta_name = "Rigol_DL3021"
            elif serial_upper.startswith("DP8B") or serial_upper.startswith("DP83"):
                meta_name = "Rigol_DP832"
            elif serial_upper.startswith("DP8H") or serial_upper.startswith("DP81"):
                meta_name = "Rigol_DP811"
            elif serial_upper.startswith("DP82") or serial_upper.startswith("DP8G"):
                meta_name = "Rigol_DP821"
            else:
                meta_name = "Rigol_DP821"
        else:
            meta_name = _VIDPID_TO_NAME.get((vid, pid))
            if meta_name is None:
                continue

        meta = SUPPORTED_USB[meta_name]

        if meta_name == "Nordic_PPK2":
            address = f"ppk2:{serial or ''}"
        else:
            address = f"USB0::0x{vid.upper()}::0x{pid.upper()}::{serial or ''}::INSTR"

        entry: dict = {
            "name": meta_name,
            "vid": vid,
            "pid": pid,
            "serial": serial,
            "address": address,
            "net_type": meta["net_type"],
        }

        if meta_name in CHANNEL_MAPS:
            entry["channels"] = CHANNEL_MAPS[meta_name]

        if "uart" in meta.get("net_type", []):
            # Resolve actual /dev/tty* paths so consumers (TUI, dispatcher)
            # don't have to invent bridge identifiers from raw interface
            # indices. Prefer the serial-keyed walk; fall back to the
            # sysfs-device-keyed walk when the chip has no programmed
            # serial (common on bare-bones FT4232H/FT2232H modules). The
            # caveat for the fallback is that multiple identical chips
            # without serials can't be told apart by the regex-based
            # address parser, so we keep them addressable here but the
            # rest of the stack will still need a unique serial to drive
            # OpenOCD / J-Link concurrently.
            if serial:
                ttys = _get_ttys_for_usb_serial(serial) or []
            else:
                ttys = _get_ttys_for_usb_device(dev) or []
            if ttys:
                # Sort by interface number so channel A is always first.
                # Multi-channel FTDIs (FT2232H, FT4232H) get one UART
                # option per interface; single-channel devices collapse
                # to a single entry.
                ttys.sort(key=lambda t: (t.get("interface", 0), t.get("path", "")))
                uart_channels = [t["path"] for t in ttys]
                if "channels" in entry:
                    entry["channels"]["uart"] = uart_channels
                else:
                    entry["channels"] = {"uart": uart_channels}
                # tty_path stays single-valued for back-compat with
                # consumers that only look at the primary interface.
                entry["tty_path"] = uart_channels[0]
                entry["tty_paths"] = uart_channels
            elif meta.get("net_type") == ["uart"]:
                # UART-only chip with no enumerable tty — skip entirely.
                continue
            else:
                # Multi-role chip (FT4232H, FT2232H, FT232H) without any
                # enumerable tty. Drop the UART role entirely so the TUI
                # never offers a UART option that can't be addressed.
                # Other roles (debug/spi/i2c) keep working.
                if "uart" in entry.get("channels", {}):
                    entry["channels"] = {
                        k: v for k, v in entry["channels"].items() if k != "uart"
                    }

        results.append(entry)

    return results


# ---------------------------------------------------------------------------
#  Camera detection
# ---------------------------------------------------------------------------

_CAM_VID = "046d"
_CAM_PIDS = {"085e", "0856", "0843"}


def _by_camera() -> List[dict]:
    usb_cams = [
        dev for dev in scan_usb()
        if dev["vid"] == _CAM_VID and dev["pid"] in _CAM_PIDS
    ]
    if not usb_cams:
        return []

    video_nodes = sorted(
        (Path(p) for p in glob.glob("/dev/video*")),
        key=lambda p: int(p.name.replace("video", "")),
    )

    results: List[dict] = []
    for idx, cam in enumerate(usb_cams):
        try:
            video_dev = str(video_nodes[idx * 4])
        except IndexError:
            continue
        results.append({**cam, "channels": {"webcam": [video_dev]}})
    return results


# ---------------------------------------------------------------------------
#  Dexarm (robotic arm) handshake detection
# ---------------------------------------------------------------------------

_DEX_BAUD = 115200


@with_timeout(seconds=10, default=[])
def _by_handshake(*, exclude: Optional[set] = None) -> List[dict]:
    try:
        from serial import Serial, SerialException
    except ImportError:
        return []

    results = []
    exclude = exclude or set()
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))

    for port in ports:
        if port in exclude:
            continue
        try:
            with Serial(port, _DEX_BAUD, timeout=1) as ser:
                time.sleep(0.01)
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                ser.write(b"M105\n")
                time.sleep(0.01)
                resp = ser.read_all().decode(errors="ignore")
                if "ok" not in resp.lower() and "dexarm" not in resp.lower():
                    continue
        except Exception:
            continue

        serial_number = _get_serial_by_port(port)
        if not serial_number:
            continue

        results.append({
            "name": "Rotrix_Dexarm",
            "address": f"USB0::0x0483::0x5740::{serial_number}::INSTR",
            "net_type": ["arm"],
            "channels": {"arm": [port]},
        })
    return results


def _get_serial_by_port(port: str) -> Optional[str]:
    try:
        output = subprocess.check_output(
            ["udevadm", "info", "-q", "all", "-n", port],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        for key in ["ID_SERIAL_SHORT", "ID_SERIAL", "ID_USB_SERIAL"]:
            match = re.search(fr"{key}=(\w+)", output)
            if match:
                return match.group(1)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
#  Merge helper
# ---------------------------------------------------------------------------

def merge_or_append(entry: dict, instruments: List[dict]) -> None:
    """Merge *entry* into *instruments* by VID+PID+serial, or append."""
    for existing in instruments:
        if (existing.get("vid") == entry.get("vid") and
                existing.get("pid") == entry.get("pid") and
                existing.get("serial") == entry.get("serial")):
            if "channels" in entry:
                existing.setdefault("channels", {})
                for net, ch in entry["channels"].items():
                    existing["channels"].setdefault(net, [])
                    for c in ch:
                        if c not in existing["channels"][net]:
                            existing["channels"][net].append(c)
            n1 = existing.get("net_type", [])
            n2 = entry.get("net_type", [])
            if not isinstance(n1, list):
                n1 = [n1]
            if not isinstance(n2, list):
                n2 = [n2]
            existing["net_type"] = list(dict.fromkeys(n1 + n2))
            return
    instruments.append(entry)


# ---------------------------------------------------------------------------
#  Custom (user-assigned) devices
# ---------------------------------------------------------------------------
# A cable the scanner can't classify on its own (e.g. a Rigol DP711 reached
# through a generic Prolific USB-serial adapter) can be manually assigned to a
# catalog instrument with ``lager nets assign``. Assignments persist in
# /etc/lager/custom_devices.json; each one whose cable is currently plugged in
# is surfaced here as a synthetic instrument record so the normal ``nets add``
# / TUI flows light up without special-casing.
# Keep in sync with ``cli/impl/query_instruments.py`` (same duplication
# tech-debt as SUPPORTED_USB / CHANNEL_MAPS).

def custom_instruments() -> List[dict]:
    """Synthetic instrument records for live custom-device assignments."""
    if _custom_store is None:
        return []
    results: List[dict] = []
    for rec in _custom_store.load():
        entry = _catalog.get_device(rec.get("instrument"))
        if not entry:
            # Stale assignment (instrument no longer in the catalog) — skip.
            continue
        tty = _serial_id.resolve_tty(
            rec.get("vid"), rec.get("pid"),
            serial=rec.get("serial"), port_path=rec.get("port_path"),
        )
        if not tty:
            # Cable not currently plugged in; the scan only reports live HW.
            continue
        try:
            address = _custom_store.address_for(rec)
        except ValueError:
            continue
        results.append({
            "name": rec["instrument"],
            "vid": rec.get("vid"),
            "pid": rec.get("pid"),
            "serial": rec.get("serial"),
            "address": address,
            "net_type": list(entry.get("roles", [])),
            # Copy per-role lists so callers can't mutate the catalog.
            "channels": {role: list(chs)
                         for role, chs in (entry.get("channels") or {}).items()},
            "tty_path": tty,
            "custom": True,
        })
    return results


def _apply_custom_devices(instruments: List[dict], custom: List[dict]) -> List[dict]:
    """Add custom-device records, replacing their generic cable records.

    An assigned cable must not also be offered as a generic UART adapter —
    both records would point at the same tty, and a UART net opening it would
    fight the instrument driver. Only UART-only entries are suppressed;
    multi-role chips (e.g. FTDI debug+uart) keep their generic record.
    """
    if not custom:
        return instruments
    custom_ttys = {dev["tty_path"] for dev in custom if dev.get("tty_path")}
    kept = []
    for inst in instruments:
        ttys = set(inst.get("tty_paths") or [])
        if inst.get("tty_path"):
            ttys.add(inst["tty_path"])
        if inst.get("net_type") == ["uart"] and ttys & custom_ttys:
            continue
        kept.append(inst)
    kept.extend(custom)
    return kept


# ---------------------------------------------------------------------------
#  Top-level scan
# ---------------------------------------------------------------------------

def list_instruments() -> List[dict]:
    """Return all detected instruments, sorted by name."""
    custom = custom_instruments()
    instruments = scan_usb()
    # Custom-assigned cables join the handshake exclusion set: writing G-code
    # at a bench instrument (e.g. a DP711 supply) could actuate it.
    uart_ports = {dev.get("tty_path") for dev in instruments if dev.get("tty_path")}
    uart_ports |= {dev["tty_path"] for dev in custom if dev.get("tty_path")}
    for dex in _by_handshake(exclude=uart_ports):
        merge_or_append(dex, instruments)
    for cam in _by_camera():
        merge_or_append(cam, instruments)
    instruments = _apply_custom_devices(instruments, custom)
    instruments.sort(key=lambda d: (d["name"], d.get("address", "")))
    return instruments
