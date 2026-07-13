# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import json
import time
import os
import sys
import re
import glob
import signal
from pathlib import Path
from typing import List, Dict, Optional, Callable, TypeVar, Any
from serial import Serial, SerialException
import subprocess
from collections import defaultdict

try:
    # Box-side custom-device framework. This script executes on the box, where
    # the box's ``lager`` package is importable (same mechanism as net.py's
    # ``from lager.nets.net_cli import _cli``). Older box images predate
    # ``lager.devices`` — degrade to "no custom devices" rather than failing
    # the whole scan.
    from lager.devices import catalog as _catalog
    from lager.devices import custom_store as _custom_store
    from lager.devices import serial_id as _serial_id
except Exception:
    _catalog = _custom_store = _serial_id = None

T = TypeVar('T')

# ---------------------------------------------------------------------------
#  Timeout Helper
# ---------------------------------------------------------------------------

class _ScanTimeoutError(Exception):
    """Raised when a USB scan operation times out."""
    pass

def timeout_handler(signum, frame):
    """Signal handler for timeout."""
    raise _ScanTimeoutError("Operation timed out")

def with_timeout(seconds: int, default: T = None) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator to add a timeout to a function.
    If the function takes longer than `seconds`, return `default`.

    Note: Uses SIGALRM which only works on Unix-like systems.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args, **kwargs) -> T:
            # Set up the signal handler
            old_handler = signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(seconds)
            try:
                result = func(*args, **kwargs)
                signal.alarm(0)  # Cancel the alarm
                return result
            except _ScanTimeoutError:
                return default
            finally:
                signal.alarm(0)  # Ensure alarm is cancelled
                signal.signal(signal.SIGALRM, old_handler)  # Restore old handler
        return wrapper
    return decorator

# ---------------------------------------------------------------------------
#  Static USB table
# ---------------------------------------------------------------------------
SUPPORTED_USB: Dict[str, Dict[str, str | List[str] | None]] = {
    # supply
    "Rigol_DP811":       {"vid": "1ab1", "pid": "0e11", "net_type": ["power-supply"]}, # Same VID:PID as DP821, differentiated by serial
    "Rigol_DP821":       {"vid": "1ab1", "pid": "0e11", "net_type": ["power-supply"]},
    "Rigol_DP831":       {"vid": "1ab1", "pid": "0e31", "net_type": ["power-supply"]},
    "Rigol_DP832":       {"vid": "1ab1", "pid": "0e11", "net_type": ["power-supply"]}, # Same VID:PID as DP821, differentiated by serial
    "EA_PSB_10080_60":   {"vid": "232e", "pid": "0053", "net_type": ["power-supply", "solar"]},
    "EA_PSB_10060_60":   {"vid": "232e", "pid": "0053", "net_type": ["power-supply", "solar"]},
    "KEYSIGHT_E36233A":  {"vid": "2a8d", "pid": "3302", "net_type": ["power-supply"]}, # 2-channel
    "KEYSIGHT_E36313A":  {"vid": "2a8d", "pid": "1202", "net_type": ["power-supply"]},
    "KEYSIGHT_E36312A":  {"vid": "2a8d", "pid": "1102", "net_type": ["power-supply"]},

    # battery
    "Keithley_2281S":    {"vid": "05e6", "pid": "2281", "net_type": ["battery", "power-supply"]},

    # scope
    "Rigol_MS05204":     {"vid": "1ab1", "pid": "0515", "net_type": ["scope"]},
    "Picoscope_2000":    {"vid": "0ce9", "pid": "1007", "net_type": ["scope"]},

    # adc / gpio / dac / spi
    "LabJack_T7":        {"vid": "0cd5", "pid": "0007", "net_type": ["gpio", "adc", "dac", "spi", "i2c"]},
    "Aardvark":          {"vid": "0403", "pid": "e0d0", "net_type": ["spi", "i2c", "gpio"]},
    # FT232H — single channel; can run in MPSSE mode (spi/i2c/gpio/debug via
    # libftdi) OR async-serial mode (uart via ``ftdi_sio``), but not both
    # simultaneously. Users pick at most one role per FT232H.
    "FTDI_FT232H":       {"vid": "0403", "pid": "6014", "net_type": ["spi", "i2c", "gpio", "debug", "uart"]},
    # FT2232H — two MPSSE channels (A and B). Channel A is typically wired to
    # JTAG/SWD, channel B is free for SPI/I2C/GPIO or UART. The OpenOCD
    # backend reads the FTDI channel index out of the debug net's ``device``
    # field (``STM32F4x@A``) or its ``probe_channel`` field.
    "FTDI_FT2232H":      {"vid": "0403", "pid": "6010", "net_type": ["spi", "i2c", "gpio", "debug", "uart"]},
    "MCC_USB-202":       {"vid": "09db", "pid": "012b", "net_type": ["adc", "dac", "gpio"]},

    # debug — J-Link family (handled by the J-Link backend on the box)
    "J-Link":                       {"vid": "1366", "pid": "1024", "net_type": ["debug"]},
    "J-Link_Plus":                  {"vid": "1366", "pid": "0101", "net_type": ["debug"]},
    "J-Link_Base_Compact":          {"vid": "1366", "pid": "1020", "net_type": ["debug"]},
    "Flasher_ARM":                  {"vid": "1366", "pid": "0503", "net_type": ["debug"]},
    "J-Link_Flasher_Pro":           {"vid": "1366", "pid": "0105", "net_type": ["debug"]},
    # debug — OpenOCD-backed probes
    "STLink_v2":                    {"vid": "0483", "pid": "3748", "net_type": ["debug"]},
    "STLink_v2_1":                  {"vid": "0483", "pid": "374b", "net_type": ["debug"]},
    "STLink_v3_Mini":               {"vid": "0483", "pid": "374d", "net_type": ["debug"]},
    "STLink_v3":                    {"vid": "0483", "pid": "374e", "net_type": ["debug"]},
    "STLink_v3_2VCP":               {"vid": "0483", "pid": "374f", "net_type": ["debug"]},
    "RP2040_Picoprobe":             {"vid": "2e8a", "pid": "000c", "net_type": ["debug"]},
    "Atmel_EDBG":                   {"vid": "03eb", "pid": "2111", "net_type": ["debug"]},
    "DAPLink":                      {"vid": "0d28", "pid": "0204", "net_type": ["debug"]},

    # usb
    "Acroname_8Port":     {"vid": "24ff", "pid": "0013", "net_type": ["usb"]},
    "Acroname_4Port":     {"vid": "24ff", "pid": "0011", "net_type": ["usb"]},
    "YKUSH_Hub":         {"vid": "04d8", "pid": "f2f7", "net_type": ["usb"]},

    # eload
    "Rigol_DL3021":      {"vid": "1ab1", "pid": "0e11", "net_type": ["eload"]},  # Same PID as DP821, differentiated by serial

    # camera — detection is catalog-driven: any entry with a ``webcam``
    # net_type is picked up by ``_by_camera`` below, so adding a camera
    # is a one-line addition here (mirror it in box/lager/http_handlers/usb_scanner.py).
    "Logitech_BRIO_HD":  {"vid": "046d", "pid": "085e", "net_type": ["webcam"]},
    "Logitech_BRIO":     {"vid": "046d", "pid": "0856", "net_type": ["webcam"]},
    "Logitech_BRIO_4K_Stream": {"vid": "046d", "pid": "086b", "net_type": ["webcam"]},
    "Logitech_4K_Pro":   {"vid": "046d", "pid": "087f", "net_type": ["webcam"]},
    "Logitech_C930e":    {"vid": "046d", "pid": "0843", "net_type": ["webcam"]},
    "Logitech_C925e":    {"vid": "046d", "pid": "085b", "net_type": ["webcam"]},
    "Logitech_C922_Pro": {"vid": "046d", "pid": "085c", "net_type": ["webcam"]},
    "Logitech_C920":     {"vid": "046d", "pid": "082d", "net_type": ["webcam"]},
    "Logitech_C615":     {"vid": "046d", "pid": "082c", "net_type": ["webcam"]},
    "Logitech_C270":     {"vid": "046d", "pid": "0825", "net_type": ["webcam"]},
    "Logitech_StreamCam": {"vid": "046d", "pid": "0893", "net_type": ["webcam"]},

    # watt-meter
    "Yocto_Watt": {"vid": "24e0", "pid": "002a", "net_type": ["watt-meter"]},
    "Joulescope_JS220": {"vid": "16d0", "pid": "10ba", "net_type": ["watt-meter", "energy-analyzer"]},
    "Nordic_PPK2": {"vid": "1915", "pid": "c00a", "net_type": ["watt-meter", "energy-analyzer"]},

    # thermocouple
    "Phidget": {"vid": "06c2", "pid": "0046", "net_type": ["thermocouple"]},

    # uart
    "Prolific_USB_Serial": {"vid": "067b", "pid": "23a3", "net_type": ["uart"]},
    "SiLabs_CP210x": {"vid": "10c4", "pid": "ea60", "net_type": ["uart"]},
    "FTDI_FT232R": {"vid": "0403", "pid": "6001", "net_type": ["uart"]},
    # FT4232H — four channels. A and B can run MPSSE (JTAG/SWD via OpenOCD);
    # C and D are UART-only. Pick the interface via the ``@A``..``@D`` suffix.
    "FTDI_FT4232H": {"vid": "0403", "pid": "6011", "net_type": ["uart", "debug"]},
    "ESP32_JTAG_Serial": {"vid": "303a", "pid": "1001", "net_type": ["uart"]},
}

# ── Channel maps ────────────────────────────────────────────

CHANNEL_MAPS = {
    # supply
    "Rigol_DP811":            {"power-supply": ["1"]},
    "Rigol_DP821":            {"power-supply": ["1", "2"]},
    "Rigol_DP831":            {"power-supply": ["1", "2", "3"]},
    "Rigol_DP832":            {"power-supply": ["1", "2", "3"]},
    "EA_PSB_10080_60":        {"power-supply": ["1"], "solar": ["1"]},
    "EA_PSB_10060_60":        {"power-supply": ["1"], "solar": ["1"]},
    "KEYSIGHT_E36233A":       {"power-supply": ["1", "2"]},
    "KEYSIGHT_E36313A":       {"power-supply": ["1", "2", "3"]},
    "KEYSIGHT_E36312A":       {"power-supply": ["1", "2", "3"]},

    # battery
    "Keithley_2281S":         {"power-supply": ["1"], "battery": ["1"]},

    # scope
    "Picoscope_2000":         {"scope": ["1", "2"]},
    "Rigol_MS05204":          {"scope": ["1", "2", "3", "4"],  "logic": ["1"]},

    # adc / gpio / dac
    "LabJack_T7": {
        "gpio": [
            "CIO0","CIO1","CIO2","CIO3",
            "EIO0","EIO1","EIO2","EIO3","EIO4","EIO5","EIO6","EIO7",
            "MIO0","MIO1","MIO2",
            "FIO0","FIO1","FIO2","FIO3","FIO4","FIO5","FIO6","FIO7",
        ],
        "adc": [
            "AIN0","AIN1","AIN2","AIN3","AIN4","AIN5","AIN6","AIN7",
            "AIN8","AIN9","AIN10","AIN11","AIN12","AIN13",
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
        # UART channel(s) populated by the tty scan (``/dev/ttyUSB<N>``).
        "uart": [],
    },
    "FTDI_FT2232H": {
        "spi": ["SPI0"],
        "i2c": ["I2C0"],
        "gpio": ["4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"],
        # ``DEVICE_TYPE@<channel>`` placeholders: the CLI prompts replace the
        # ``DEVICE_TYPE`` part with a real MCU name, leaving ``@A``/``@B``
        # intact so the OpenOCD backend can route to the right interface.
        "debug": ["DEVICE_TYPE@A", "DEVICE_TYPE@B"],
        # UART tty paths are populated at scan time (same as FT232H); empty
        # default avoids handing the TUI raw interface indices like ``0``
        # that would later land in the saved net's ``pin`` field and break
        # the UART dispatcher's serial lookup.
        "uart": [],
    },
    "FTDI_FT4232H": {
        # A and B can drive JTAG/SWD via OpenOCD; C and D are UART-only.
        "debug": ["DEVICE_TYPE@A", "DEVICE_TYPE@B"],
        # See FT2232H note above — placeholder list is filled with real
        # ``/dev/ttyUSB*`` paths by the scanner; if enumeration fails the
        # role is dropped entirely rather than advertised as a bare index.
        "uart": [],
    },

    # debug — J-Link family
    "J-Link":                 {"debug": ["DEVICE_TYPE"]},
    "J-Link_Plus":            {"debug": ["DEVICE_TYPE"]},
    "J-Link_Base_Compact":    {"debug": ["DEVICE_TYPE"]},
    "Flasher_ARM":            {"debug": ["DEVICE_TYPE"]},
    "J-Link_Flasher_Pro":     {"debug": ["DEVICE_TYPE"]},
    # debug — OpenOCD-backed probes
    "STLink_v2":              {"debug": ["DEVICE_TYPE"]},
    "STLink_v2_1":            {"debug": ["DEVICE_TYPE"]},
    "STLink_v3_Mini":         {"debug": ["DEVICE_TYPE"]},
    "STLink_v3":              {"debug": ["DEVICE_TYPE"]},
    "STLink_v3_2VCP":         {"debug": ["DEVICE_TYPE"]},
    "RP2040_Picoprobe":       {"debug": ["DEVICE_TYPE"]},
    "Atmel_EDBG":             {"debug": ["DEVICE_TYPE"]},
    "DAPLink":                {"debug": ["DEVICE_TYPE"]},

    # usb
    "Acroname_8Port":          {"usb": ["0", "1", "2", "3", "4", "5", "6", "7"]},
    "Acroname_4Port":          {"usb": ["0", "1", "2", "3"]},
    "YKUSH_Hub":              {"usb": ["1", "2", "3"]},

    # eload
    "Rigol_DL3021":           {"eload": ["1"]},

    # watt-meter
    "Yocto_Watt":             {"watt-meter": ["0"]},
    "Joulescope_JS220":       {"watt-meter": ["0"], "energy-analyzer": ["0"]},
    "Nordic_PPK2":            {"watt-meter": ["0"], "energy-analyzer": ["0"]},

    # thermocouple
    "Phidget":                {"thermocouple": ["0", "1", "2", "3"]},

    # uart
    "Prolific_USB_Serial":    {"uart": ["0"]},
    "SiLabs_CP210x":          {"uart": ["0"]},
    "FTDI_FT232R":            {"uart": ["0"]},
    "ESP32_JTAG_Serial":      {"uart": ["0"]},
}

# ---------------------------------------------------------------------------
#  Dexarm Helpers
# ---------------------------------------------------------------------------

DEX_VID = "0483"
DEX_PID = "5740"
DEX_BAUD = 115200

def get_serial_by_port(port):
    """Attempt to get the USB serial number for a device."""
    try:
        output = subprocess.check_output(
            ["udevadm", "info", "-q", "all", "-n", port],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2  # 2 second timeout for udevadm
        )
        for key in ["ID_SERIAL_SHORT", "ID_SERIAL", "ID_USB_SERIAL"]:
            match = re.search(fr"{key}=(\w+)", output)
            if match:
                return match.group(1)

        output = subprocess.check_output(
            ["udevadm", "info", "-a", "-n", port],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2  # 2 second timeout for udevadm
        )
        match = re.search(r'ATTRS{serial}=="([^"]+)"', output)
        if match:
            return match.group(1)

    except subprocess.TimeoutExpired:
        print(f"Timeout retrieving serial for {port}", file=sys.stderr)
    except Exception as e:
        print(f"Error retrieving serial for {port}: {e}", file=sys.stderr)
    return None

@with_timeout(seconds=10, default=[])
def _by_handshake(*, exclude: Optional[set[str]] = None) -> List[dict]:
    """
    Return all Dexarms currently attached, using handshake probe.

    Probes serial ports with G-code commands to detect Rotrics Dexarm robotic arms.
    Wrapped with 10-second timeout to prevent hanging on problematic devices.

    Args:
        exclude: Set of device paths to skip (e.g., already-identified UART adapters)
    """
    results = []
    exclude = exclude or set()
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))

    for port in ports:
        if port in exclude:
            continue

        try:
            with Serial(port, DEX_BAUD, timeout=1) as ser:
                time.sleep(0.01)
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                ser.write(b"M105\n")  # harmless G-code
                time.sleep(0.01)
                resp = ser.read_all().decode(errors="ignore")

                if "ok" not in resp.lower() and "dexarm" not in resp.lower():
                    continue
        except SerialException:
            continue
        except Exception:
            continue

        serial_number = get_serial_by_port(port)
        if not serial_number:
            continue

        results.append({
            "name": f"Rotrix_Dexarm",
            "address": f"USB0::0x0483::0x5740::{serial_number}::INSTR",
            "net_type": ["arm"],
            "channels": {"arm": [port]},
        })

    return results

# ---------------------------------------------------------------------------
#  Camera detection
# ---------------------------------------------------------------------------

_WEBCAM_VIDPIDS = {
    (meta["vid"].lower(), meta["pid"].lower())
    for meta in SUPPORTED_USB.values()
    if "webcam" in meta["net_type"]
}

def _read_sysfs_attr(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError:
        return None

def _by_camera(v4l_root: Path = Path("/sys/class/video4linux")) -> List[dict]:
    """Detect supported webcams and their /dev/video capture nodes.

    Each ``/sys/class/video4linux/videoN`` links to the USB interface that
    owns it; the interface's parent directory is the USB device carrying
    idVendor/idProduct/serial. Walking that link keeps the node→camera
    mapping correct when models expose different numbers of video nodes
    (a C920 exposes two, a BRIO four), which an index-based heuristic
    cannot get right on mixed setups.
    """
    results: List[dict] = []
    seen_devices: set = set()
    video_nodes = sorted(
        v4l_root.glob("video*"),
        key=lambda p: int(p.name.replace("video", ""))
    )
    for node in video_nodes:
        try:
            usb_dev = (node / "device").resolve().parent
        except OSError:
            continue
        vid = _read_sysfs_attr(usb_dev / "idVendor")
        pid = _read_sysfs_attr(usb_dev / "idProduct")
        if not vid or not pid:
            continue
        vid, pid = vid.lower(), pid.lower()
        if (vid, pid) not in _WEBCAM_VIDPIDS:
            continue
        if str(usb_dev) in seen_devices:
            # Higher-numbered nodes on the same camera are metadata/IR
            # streams; the first node is the capture stream.
            continue
        seen_devices.add(str(usb_dev))
        serial = _read_sysfs_attr(usb_dev / "serial")
        results.append({
            "name": _VIDPID_TO_NAME[(vid, pid)],
            "vid": vid,
            "pid": pid,
            "serial": serial,
            "address": f"USB0::0x{vid.upper()}::0x{pid.upper()}::{serial or ''}::INSTR",
            "net_type": ["webcam"],
            "channels": {"webcam": [f"/dev/{node.name}"]},
        })
    return results

def _merge_or_append(entry: dict, instruments: List[dict]) -> None:
    """
    If an instrument with the same VID, PID and serial already exists
    in `instruments`, merge `channels` and `net_type` into it.
    Otherwise append the new entry.
    """
    for existing in instruments:
        if (existing.get("vid") == entry.get("vid") and
            existing.get("pid") == entry.get("pid") and
            existing.get("serial") == entry.get("serial")):

            # ── merge channels ───────────────────────────────────────
            if "channels" in entry:
                existing.setdefault("channels", {})
                for net, ch in entry["channels"].items():
                    existing["channels"].setdefault(net, [])
                    for c in ch:
                        if c not in existing["channels"][net]:
                            existing["channels"][net].append(c)

            # ── merge / deduplicate net_type ─────────────────────────
            n1 = existing.get("net_type", [])
            n2 = entry.get("net_type", [])
            if not isinstance(n1, list): n1 = [n1]
            if not isinstance(n2, list): n2 = [n2]
            existing["net_type"] = list(dict.fromkeys(n1 + n2))
            return

    instruments.append(entry)

# ---------------------------------------------------------------------------
#  Custom (user-assigned) devices
# ---------------------------------------------------------------------------
# A cable the scanner can't classify on its own (e.g. a Rigol DP711 reached
# through a generic Prolific USB-serial adapter) can be manually assigned to a
# catalog instrument with ``lager nets assign``. Assignments persist in
# /etc/lager/custom_devices.json on the box; each one whose cable is currently
# plugged in is surfaced here as a synthetic instrument record so the normal
# ``nets add`` / TUI flows light up without special-casing.
# Keep in sync with ``box/lager/http_handlers/usb_scanner.py`` (same
# duplication tech-debt as SUPPORTED_USB / CHANNEL_MAPS).

def _custom_instruments() -> List[dict]:
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
#  UART USB-Serial Helpers
# ---------------------------------------------------------------------------

@with_timeout(seconds=2, default=None)
def _get_tty_for_usb_serial(serial_number: Optional[str] = None) -> Optional[str]:
    """
    Find the /dev/tty* device path for a USB serial adapter by serial number.
    Uses sysfs to map USB device serial to tty device.

    Wrapped with 2-second timeout to prevent hanging on problematic devices
    (e.g., composite USB devices like ESP32 with complex sysfs structures).

    For multi-channel FTDI chips (FT2232H, FT4232H) this returns the FIRST
    matching tty (sorted by interface number). Use
    :func:`_get_ttys_for_usb_serial` to enumerate every interface.
    """
    ttys = _get_ttys_for_usb_serial(serial_number) or []
    if not ttys:
        return None
    ttys.sort(key=lambda t: (t.get("interface", 0), t.get("path", "")))
    return ttys[0]["path"]


# Parses interface numbers out of sysfs USB paths like ``3-1:1.2`` (interface
# 2 on USB device 3-1). The colon separates device address from config:iface.
_USB_INTERFACE_RE = re.compile(r":\d+\.(\d+)$")


@with_timeout(seconds=2, default=None)
def _get_ttys_for_usb_serial(serial_number: Optional[str] = None):
    """Return ``[{'path', 'interface'}]`` for every tty bound to *serial_number*.

    Multi-channel FTDIs (FT2232H, FT4232H) expose one USB interface per
    MPSSE/UART channel, each backed by its own ``/dev/ttyUSB<N>``. We walk
    every tty, traverse parents until the USB device's ``serial`` matches,
    and tag each match with the interface number parsed from the
    ``...:1.<iface>`` path component.
    """
    if not serial_number:
        return []
    return _walk_ttys(match=lambda usb_dir: _read_sysfs_text(usb_dir / "serial") == serial_number)


@with_timeout(seconds=2, default=None)
def _get_ttys_for_usb_device(usb_device_path):
    """Return ``[{'path', 'interface'}]`` for every tty under *usb_device_path*.

    Sibling of :func:`_get_ttys_for_usb_serial` for the case where the
    physical USB device has no programmed serial number — typical for
    bare FT2232H/FT4232H chips whose EEPROM was never burnt. We match
    against the device's sysfs node instead of its ``serial`` file so
    the UART role can still be enumerated; the resulting ``/dev/tty*``
    paths land in the saved net's ``pin`` field and round-trip cleanly
    through the box-side UART dispatcher's device-path fast path.

    Ambiguity caveat: multiple identical chips of the same VID:PID
    without serials can't be told apart by VISA address, so the rest of
    the stack will still need a unique serial to drive concurrent
    OpenOCD / J-Link sessions.
    """
    if not usb_device_path:
        return []
    try:
        target = usb_device_path.resolve()
    except OSError:
        return []
    return _walk_ttys(match=lambda usb_dir: usb_dir == target)


def _read_sysfs_text(path) -> Optional[str]:
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
    truthy wins; we also stop walking once we cross the USB device
    root (the directory containing ``idVendor``) so we don't climb into
    the host controller and trigger spurious matches.
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
#  USB Helpers
# ---------------------------------------------------------------------------

_VIDPID_TO_NAME: Dict[tuple[str, str], str] = {}
# Special handling for instruments with duplicate VID:PID
# Skip instruments that share VID:PID 1ab1:0e11 - they need serial-based detection
for _name, meta in SUPPORTED_USB.items():
    if _name in ("Rigol_DL3021", "Rigol_DP811", "Rigol_DP832"):
        continue  # Handle these specially by serial number (share VID:PID with DP821)
    _VIDPID_TO_NAME[(meta["vid"].lower(), meta["pid"].lower())] = _name


def _scan_usb() -> List[dict]:
    """Quick VID:PID scan via /sys (Linux only) with channel map for LabJack."""
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

            # Use non-blocking reads to avoid hanging on problematic devices
            vid_fd = os.open(str(vid_path), os.O_RDONLY | os.O_NONBLOCK)
            vid = os.read(vid_fd, 64).decode('utf-8').strip().lower()
            os.close(vid_fd)

            pid_fd = os.open(str(pid_path), os.O_RDONLY | os.O_NONBLOCK)
            pid = os.read(pid_fd, 64).decode('utf-8').strip().lower()
            os.close(pid_fd)

            # Check if this device is supported BEFORE reading serial
            # (avoids reading serial from unsupported devices that might hang)
            if (vid, pid) not in _VIDPID_TO_NAME and not (vid == "1ab1" and pid == "0e11"):
                continue

            serial = None
            if serial_path.exists():
                try:
                    serial_fd = os.open(str(serial_path), os.O_RDONLY | os.O_NONBLOCK)
                    serial = os.read(serial_fd, 256).decode('utf-8').strip()
                    os.close(serial_fd)
                except (OSError, UnicodeDecodeError):
                    serial = None
        except (OSError, UnicodeDecodeError, BlockingIOError):
            continue

        # Special handling for Rigol instruments with same VID:PID (1ab1:0e11)
        # Differentiate by serial number prefix
        if vid == "1ab1" and pid == "0e11" and serial:
            serial_upper = serial.upper()
            if serial_upper.startswith("DL3"):
                meta_name = "Rigol_DL3021"  # DL3000 series electronic load
            elif serial_upper.startswith("DP8B") or serial_upper.startswith("DP83"):
                meta_name = "Rigol_DP832"  # DP832/DP832A - 3 channel power supply
            elif serial_upper.startswith("DP8H") or serial_upper.startswith("DP81"):
                meta_name = "Rigol_DP811"  # DP811/DP811A - 1 channel power supply
            elif serial_upper.startswith("DP82") or serial_upper.startswith("DP8G"):
                meta_name = "Rigol_DP821"  # DP821 - 2 channel power supply
            elif serial_upper.startswith("DP8"):
                # Generic DP8xx - default to DP821
                meta_name = "Rigol_DP821"
            else:
                # Default to power supply if we can't determine from serial
                meta_name = "Rigol_DP821"
        else:
            meta_name = _VIDPID_TO_NAME.get((vid, pid))
            if meta_name is None:
                continue

        meta = SUPPORTED_USB[meta_name]

        # PPK2 uses ppk2-api (USB CDC serial), not VISA/USBTMC.
        # Store a ppk2:{serial} address so the dispatcher's _parse_location()
        # can find the device via ppk2_api.list_devices().
        if meta_name == "Nordic_PPK2":
            address = f"ppk2:{serial or ''}"
        else:
            address = f"USB0::0x{vid.upper()}::0x{pid.upper()}::{serial or ''}::INSTR"

        entry = {
            "name": meta_name,
            "vid": vid,
            "pid": pid,
            "serial": serial,
            "address": address,
            "net_type": meta["net_type"],
        }

        if meta_name in CHANNEL_MAPS:
            entry["channels"] = CHANNEL_MAPS[meta_name]

        # UART: enumerate one channel per tty so multi-channel FTDIs (FT2232H,
        # FT4232H) can expose every interface as a separate UART net. Single-
        # channel chips collapse to a single entry. Multi-role chips (FTDI
        # with both ``debug`` and ``uart`` net_types) keep their other roles
        # intact even when no tty is currently enumerable.
        if "uart" in meta.get("net_type", []):
            # Resolve actual /dev/tty* paths so the TUI doesn't invent
            # bridge identifiers from raw interface indices. Prefer the
            # serial-keyed walk; fall back to the sysfs-device-keyed walk
            # when the chip has no programmed serial (common on bare-bones
            # FT4232H/FT2232H modules). Keep this branch in sync with
            # ``box/lager/http_handlers/usb_scanner.py``.
            if serial:
                ttys = _get_ttys_for_usb_serial(serial) or []
            else:
                ttys = _get_ttys_for_usb_device(dev) or []
            if ttys:
                ttys.sort(key=lambda t: (t.get("interface", 0), t.get("path", "")))
                uart_channels = [t["path"] for t in ttys]
                if "channels" in entry:
                    entry["channels"]["uart"] = uart_channels
                else:
                    entry["channels"] = {"uart": uart_channels}
                entry["tty_path"] = uart_channels[0]
                entry["tty_paths"] = uart_channels
            elif meta.get("net_type") == ["uart"]:
                # UART-only chip with no enumerable tty — skip entirely.
                continue
            else:
                # Multi-role chip (FT4232H, FT2232H, FT232H) without any
                # enumerable tty. Drop the UART role so the TUI never
                # offers a UART option that can't be addressed.
                if "uart" in entry.get("channels", {}):
                    entry["channels"] = {
                        k: v for k, v in entry["channels"].items() if k != "uart"
                    }

        results.append(entry)

    return results

def infer_instrument_from_address(address: str) -> str:
    address_lower = address.lower()

    # Special handling for Rigol instruments with same VID:PID (1ab1:0e11)
    if "1ab1" in address_lower and "0e11" in address_lower:
        # Extract serial from address (format: USB0::0x1AB1::0x0E11::SERIAL::INSTR)
        parts = address.split("::")
        if len(parts) > 3:
            serial = parts[3].upper()
            if serial.startswith("DL3"):
                return "Rigol_DL3021"
            elif serial.startswith("DP8B") or serial.startswith("DP83"):
                return "Rigol_DP832"
            elif serial.startswith("DP8H") or serial.startswith("DP81"):
                return "Rigol_DP811"
            elif serial.startswith("DP82") or serial.startswith("DP8G"):
                return "Rigol_DP821"
            elif serial.startswith("DP8"):
                return "Rigol_DP821"

    for name, ids in SUPPORTED_USB.items():
        vid = ids.get("vid", "").lower().replace("0x", "")
        pid = ids.get("pid", "").lower().replace("0x", "")
        if vid and pid and vid in address_lower and pid in address_lower:
            return name
    raise ValueError("Instrument for address not found")

def is_address_connected(address: str) -> bool:
    usb_devices = _scan_usb()
    # Build exclusion list from already-identified UART devices to prevent handshake probing
    uart_ports = {dev.get("tty_path") for dev in usb_devices if dev.get("tty_path")}
    connected_devices = usb_devices + _by_handshake(exclude=uart_ports)
    return any(dev.get("address") == address for dev in connected_devices)

def assert_address_is_connected(address: str):
    if not is_address_connected(address):
        print(f"Error: device with address {address} not found – is it unplugged?", file=sys.stderr)
        sys.exit(1)

def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    # Handle get_instrument command used by nets add
    if len(argv) >= 2 and argv[0] == "get_instrument":
        address = argv[1]
        custom = _custom_instruments()
        instruments: List[dict] = _scan_usb()
        # Build exclusion list from already-identified UART devices to prevent
        # handshake probing. Custom-assigned cables are excluded too: writing
        # G-code at a bench instrument (e.g. a DP711 supply) could actuate it.
        uart_ports = {dev.get("tty_path") for dev in instruments if dev.get("tty_path")}
        uart_ports |= {dev["tty_path"] for dev in custom if dev.get("tty_path")}
        for dex in _by_handshake(exclude=uart_ports):
            _merge_or_append(dex, instruments)
        for cam in _by_camera():
            _merge_or_append(cam, instruments)
        instruments = _apply_custom_devices(instruments, custom)

        # Find instrument by address
        for instrument in instruments:
            if instrument.get("address") == address:
                json.dump(instrument, sys.stdout)
                sys.stdout.write("\n")
                return

        # If not found, return empty dict
        json.dump({}, sys.stdout)
        sys.stdout.write("\n")
        return

    # Default behavior: list all instruments
    custom = _custom_instruments()
    instruments: List[dict] = _scan_usb()
    # Build exclusion list from already-identified UART devices to prevent
    # handshake probing. Custom-assigned cables are excluded too: writing
    # G-code at a bench instrument (e.g. a DP711 supply) could actuate it.
    uart_ports = {dev.get("tty_path") for dev in instruments if dev.get("tty_path")}
    uart_ports |= {dev["tty_path"] for dev in custom if dev.get("tty_path")}
    for dex in _by_handshake(exclude=uart_ports):
        _merge_or_append(dex, instruments)

    for cam in _by_camera():
        _merge_or_append(cam, instruments)

    instruments = _apply_custom_devices(instruments, custom)
    instruments.sort(key=lambda d: (d["name"], d.get("address", "")))
    json.dump(instruments, sys.stdout, indent=2)
    sys.stdout.write("\n")

if __name__ == "__main__":
    main()