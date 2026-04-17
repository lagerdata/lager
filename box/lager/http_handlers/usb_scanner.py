# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""USB instrument scanner for the Lager Box HTTP server.

Extracted from cli/impl/query_instruments.py so the scan logic can be
imported by HTTP handlers that are deployed inside the box container.
The CLI version (query_instruments.py) remains the canonical copy for
CLI usage; this module keeps the box HTTP server self-contained.

Platform support: USB and serial enumeration is delegated to lager.usb_enum,
which has Linux (sysfs) and macOS (pyusb + pyserial.list_ports) backends.
Picoscope 2000 and MCC USB-202 are explicitly skipped on macOS because their
vendor SDKs have no macOS support.
"""

import glob
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, TypeVar

from ..usb_enum import (
    get_serial_by_port,
    get_tty_for_usb_serial,
    iter_serial_ports,
    iter_usb_devices,
    iter_video_devices,
)

logger = logging.getLogger(__name__)

T = TypeVar('T')

_IS_DARWIN = sys.platform == "darwin"

# Instruments whose vendor SDKs have no macOS support — skipped on Darwin
# with a clear log line so users understand why they aren't enumerated.
_MACOS_UNSUPPORTED = {
    "MCC_USB-202",
    "Picoscope_2000",
}


# ---------------------------------------------------------------------------
#  Timeout helper (thread-safe, cross-platform)
# ---------------------------------------------------------------------------

import threading
import concurrent.futures


def with_timeout(seconds: int, default: T = None) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator that aborts a function after *seconds* and returns *default*.

    Uses concurrent.futures.ThreadPoolExecutor instead of signal.SIGALRM so
    it works from any thread on any platform. The original SIGALRM approach
    only works in the main thread on Unix and crashes on macOS when called
    from Flask worker threads.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args, **kwargs) -> T:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(func, *args, **kwargs)
                try:
                    return future.result(timeout=seconds)
                except concurrent.futures.TimeoutError:
                    return default
                except Exception:
                    return default
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
#  Static USB table (mirrors SUPPORTED_USB in query_instruments.py)
# ---------------------------------------------------------------------------

SUPPORTED_USB: Dict[str, Dict] = {
    # supply
    "Rigol_DP811":       {"vid": "1ab1", "pid": "????", "net_type": ["power-supply"]},
    "Rigol_DP821":       {"vid": "1ab1", "pid": "0e11", "net_type": ["power-supply"]},
    "Rigol_DP831":       {"vid": "1ab1", "pid": "0e31", "net_type": ["power-supply"]},
    "Rigol_DP832":       {"vid": "1ab1", "pid": "0e11", "net_type": ["power-supply"]},
    "EA_PSB_10080_60":   {"vid": "232e", "pid": "0053", "net_type": ["power-supply", "solar"]},
    "EA_PSB_10060_60":   {"vid": "232e", "pid": "0053", "net_type": ["power-supply", "solar"]},
    "KEYSIGHT_E36233A":  {"vid": "2a8d", "pid": "3302", "net_type": ["power-supply"]},
    "KEYSIGHT_E36313A":  {"vid": "2a8d", "pid": "????", "net_type": ["power-supply"]},
    "KEYSIGHT_E36312A":  {"vid": "2a8d", "pid": "1102", "net_type": ["power-supply"]},
    # battery
    "Keithley_2281S":    {"vid": "05e6", "pid": "2281", "net_type": ["battery", "power-supply"]},
    # scope
    "Rigol_MS05204":     {"vid": "1ab1", "pid": "0515", "net_type": ["scope"]},
    "Picoscope_2000":    {"vid": "0ce9", "pid": "1007", "net_type": ["scope"]},
    # adc / gpio / dac / spi / i2c
    "LabJack_T7":        {"vid": "0cd5", "pid": "0007", "net_type": ["gpio", "adc", "dac", "spi", "i2c"]},
    "Aardvark":          {"vid": "0403", "pid": "e0d0", "net_type": ["spi", "i2c", "gpio"]},
    "FTDI_FT232H":       {"vid": "0403", "pid": "6014", "net_type": ["spi", "i2c", "gpio"]},
    "MCC_USB-202":       {"vid": "09db", "pid": "012b", "net_type": ["adc", "dac", "gpio"]},
    # debug
    "J-Link":            {"vid": "1366", "pid": "1024", "net_type": ["debug"]},
    "J-Link_Plus":       {"vid": "1366", "pid": "0101", "net_type": ["debug"]},
    "Flasher_ARM":       {"vid": "1366", "pid": "0503", "net_type": ["debug"]},
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
    "FTDI_FT4232H":      {"vid": "0403", "pid": "6011", "net_type": ["uart"]},
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
    },
    "J-Link":                 {"debug": ["DEVICE_TYPE"]},
    "J-Link_Plus":            {"debug": ["DEVICE_TYPE"]},
    "Flasher_ARM":            {"debug": ["DEVICE_TYPE"]},
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
    "FTDI_FT4232H":           {"uart": ["0", "1", "2", "3"]},
    "ESP32_JTAG_Serial":      {"uart": ["0"]},
}

# VID:PID → instrument name lookup (excludes instruments with duplicate PIDs)
_VIDPID_TO_NAME: Dict[tuple, str] = {}
for _name, _meta in SUPPORTED_USB.items():
    if _name in ("Rigol_DL3021", "Rigol_DP832"):
        continue  # differentiated by serial number, handled in _scan_usb
    _VIDPID_TO_NAME[(_meta["vid"].lower(), _meta["pid"].lower())] = _name


# ---------------------------------------------------------------------------
#  UART tty helper (delegates to cross-platform usb_enum)
# ---------------------------------------------------------------------------

@with_timeout(seconds=2, default=None)
def _get_tty_for_usb_serial(serial_number: Optional[str]) -> Optional[str]:
    return get_tty_for_usb_serial(serial_number)


# ---------------------------------------------------------------------------
#  Core USB scan (cross-platform via lager.usb_enum)
# ---------------------------------------------------------------------------

def _resolve_instrument(vid: str, pid: str, serial: Optional[str]) -> Optional[str]:
    """Map a (vid, pid, serial) to a SUPPORTED_USB instrument name, or None."""
    if vid == "1ab1" and pid == "0e11" and serial:
        serial_upper = serial.upper()
        if serial_upper.startswith("DL3"):
            return "Rigol_DL3021"
        if serial_upper.startswith("DP8B") or serial_upper.startswith("DP83"):
            return "Rigol_DP832"
        if serial_upper.startswith("DP82"):
            return "Rigol_DP821"
        return "Rigol_DP821"
    return _VIDPID_TO_NAME.get((vid, pid))


def scan_usb() -> List[dict]:
    """Cross-platform VID:PID scan via lager.usb_enum."""
    results: List[dict] = []
    skipped_on_macos: set = set()

    for dev in iter_usb_devices():
        vid = dev.get("vid")
        pid = dev.get("pid")
        serial = dev.get("serial")
        if not vid or not pid:
            continue

        meta_name = _resolve_instrument(vid, pid, serial)
        if meta_name is None:
            continue

        if _IS_DARWIN and meta_name in _MACOS_UNSUPPORTED:
            skipped_on_macos.add(meta_name)
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
            if serial:
                entry["channels"] = {"uart": [serial]}
                tty_path = _get_tty_for_usb_serial(serial)
                if tty_path:
                    entry["tty_path"] = tty_path
            else:
                continue  # skip UART devices with no serial

        results.append(entry)

    if skipped_on_macos:
        logger.info(
            "Skipping unsupported instruments on macOS: %s "
            "(vendor SDK has no macOS build)",
            ", ".join(sorted(skipped_on_macos)),
        )

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

    video_nodes = iter_video_devices()
    # On Linux v4l exposes 4 nodes per camera (video0, video1, video2, video3
    # for the first camera, etc.), so the first usable node for camera N is
    # at index N*4. On macOS OpenCV returns one logical index per camera so
    # the stride is 1.
    stride = 4 if not _IS_DARWIN else 1

    results: List[dict] = []
    for idx, cam in enumerate(usb_cams):
        try:
            video_dev = video_nodes[idx * stride]
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
        from serial import Serial
    except ImportError:
        return []

    results = []
    exclude = exclude or set()
    ports = [path for path, _vid, _pid, _serial in iter_serial_ports()]

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

        serial_number = get_serial_by_port(port)
        if not serial_number:
            continue

        results.append({
            "name": "Rotrix_Dexarm",
            "address": f"USB0::0x0483::0x5740::{serial_number}::INSTR",
            "net_type": ["arm"],
            "channels": {"arm": [port]},
        })
    return results


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
#  Top-level scan
# ---------------------------------------------------------------------------

def list_instruments() -> List[dict]:
    """Return all detected instruments, sorted by name."""
    instruments = scan_usb()
    uart_ports = {dev.get("tty_path") for dev in instruments if dev.get("tty_path")}
    for dex in _by_handshake(exclude=uart_ports):
        merge_or_append(dex, instruments)
    for cam in _by_camera():
        merge_or_append(cam, instruments)
    instruments.sort(key=lambda d: (d["name"], d.get("address", "")))
    return instruments
