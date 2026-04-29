# Copyright 2024-2026 Lager Data LLC
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
    "FTDI_FT232H":       {"vid": "0403", "pid": "6014", "net_type": ["spi", "i2c", "gpio"]},
    "MCC_USB-202":       {"vid": "09db", "pid": "012b", "net_type": ["adc", "dac", "gpio"]},

    # debug
    "J-Link":                       {"vid": "1366", "pid": "1024", "net_type": ["debug"]},
    "J-Link_Plus":                  {"vid": "1366", "pid": "0101", "net_type": ["debug"]},
    "J-Link":                       {"vid": "1366", "pid": "1020", "net_type": ["debug"]},
    "Flasher_ARM":                  {"vid": "1366", "pid": "0503", "net_type": ["debug"]},
    "J-Link_Flasher_Pro":           {"vid": "1366", "pid": "0105", "net_type": ["debug"]},

    # usb
    "Acroname_8Port":     {"vid": "24ff", "pid": "0013", "net_type": ["usb"]},
    "Acroname_4Port":     {"vid": "24ff", "pid": "0011", "net_type": ["usb"]},
    "YKUSH_Hub":         {"vid": "04d8", "pid": "f2f7", "net_type": ["usb"]},

    # eload
    "Rigol_DL3021":      {"vid": "1ab1", "pid": "0e11", "net_type": ["eload"]},  # Same PID as DP821, differentiated by serial

    # camera
    "Logitech_BRIO_HD":  {"vid": "046d", "pid": "085e", "net_type": ["webcam"]},
    "Logitech_BRIO":     {"vid": "046d", "pid": "0856", "net_type": ["webcam"]},
    "Logitech_C930e":    {"vid": "046d", "pid": "0843", "net_type": ["webcam"]},

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
    "FTDI_FT4232H": {"vid": "0403", "pid": "6011", "net_type": ["uart"]},
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
    },

    # debug
    "J-Link":                 {"debug": ["DEVICE_TYPE"]},
    "J-Link_Plus":            {"debug": ["DEVICE_TYPE"]},
    "J-Link":                 {"debug": ["DEVICE_TYPE"]},
    "Flasher_ARM":            {"debug": ["DEVICE_TYPE"]},
    "J-Link_Flasher_Pro":     {"debug": ["DEVICE_TYPE"]},

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
    "FTDI_FT4232H":           {"uart": ["0", "1", "2", "3"]},
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

CAM_VID = "046d"        # Logitech
CAM_PIDS = {"085e", "0856", "0843"}   # BRIO HD / BRIO / C930e

def _by_camera() -> List[dict]:
    """
    Detect Logitech webcams (BRIO, BRIO HD, C930e) when udev attributes are missing.
    """
    usb_cams = [
        dev for dev in _scan_usb()
        if dev["vid"] == CAM_VID and dev["pid"] in CAM_PIDS
    ]

    if not usb_cams:
        return []

    video_nodes = sorted(
        (Path(p) for p in glob.glob("/dev/video*")),
        key=lambda p: int(p.name.replace("video", ""))
    )

    results: List[dict] = []
    blk_size = 4
    for idx, cam in enumerate(usb_cams):
        try:
            vidx = idx * blk_size
            video_dev = str(video_nodes[vidx])
        except IndexError:
            print(
                f"[webcam] WARNING: could not map video port for "
                f"serial {cam.get('serial') or '(no-serial)'}",
                file=sys.stderr
            )
            continue

        entry = {
            **cam,
            "channels": {"webcam": [video_dev]},
        }
        results.append(entry)

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
#  UART USB-Serial Helpers
# ---------------------------------------------------------------------------

@with_timeout(seconds=2, default=None)
def _get_tty_for_usb_serial(serial_number: Optional[str] = None) -> Optional[str]:
    """
    Find the /dev/tty* device path for a USB serial adapter by serial number.
    Uses sysfs to map USB device serial to tty device.

    Wrapped with 2-second timeout to prevent hanging on problematic devices
    (e.g., composite USB devices like ESP32 with complex sysfs structures).
    """
    if not serial_number:
        return None

    sys_tty = Path("/sys/class/tty")
    if not sys_tty.exists():
        return None

    for tty_dev in sys_tty.iterdir():
        try:
            # Skip non-USB ttys
            if not tty_dev.name.startswith(("ttyUSB", "ttyACM")):
                continue

            device_path = tty_dev / "device"
            if not device_path.exists():
                continue

            # Resolve symlink to get real device path, then navigate up to find USB device with serial number
            usb_device = device_path.resolve()
            for _ in range(10):  # Search up to 10 levels (increased from 5)
                serial_path = usb_device / "serial"
                if serial_path.exists():
                    # Use os.open with non-blocking to avoid hanging on problematic sysfs files
                    try:
                        fd = os.open(str(serial_path), os.O_RDONLY | os.O_NONBLOCK)
                        dev_serial = os.read(fd, 256).decode('utf-8').strip()
                        os.close(fd)
                        if dev_serial == serial_number:
                            return f"/dev/{tty_dev.name}"
                        break
                    except (OSError, UnicodeDecodeError, BlockingIOError):
                        # Skip devices that can't be read
                        break

                # Move up one level
                usb_device = usb_device.parent
                if not usb_device or usb_device == Path("/sys"):
                    break
        except Exception:
            continue

    return None

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

        # For UART devices, use the USB serial number as the channel identifier
        # The actual /dev/tty* path will be resolved at runtime
        if "uart" in meta.get("net_type", []):
            if serial:
                entry["channels"] = {"uart": [serial]}
                # Store the current tty path for reference/debugging
                tty_path = _get_tty_for_usb_serial(serial)
                if tty_path:
                    entry["tty_path"] = tty_path
            else:
                # Skip UART device if it has no serial number
                continue

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

    # Handle get_instrument command used by nets create
    if len(argv) >= 2 and argv[0] == "get_instrument":
        address = argv[1]
        instruments: List[dict] = _scan_usb()
        # Build exclusion list from already-identified UART devices to prevent handshake probing
        uart_ports = {dev.get("tty_path") for dev in instruments if dev.get("tty_path")}
        for dex in _by_handshake(exclude=uart_ports):
            _merge_or_append(dex, instruments)
        for cam in _by_camera():
            _merge_or_append(cam, instruments)

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
    instruments: List[dict] = _scan_usb()
    # Build exclusion list from already-identified UART devices to prevent handshake probing
    uart_ports = {dev.get("tty_path") for dev in instruments if dev.get("tty_path")}
    for dex in _by_handshake(exclude=uart_ports):
        _merge_or_append(dex, instruments)

    for cam in _by_camera():
        _merge_or_append(cam, instruments)

    instruments.sort(key=lambda d: (d["name"], d.get("address", "")))
    json.dump(instruments, sys.stdout, indent=2)
    sys.stdout.write("\n")

if __name__ == "__main__":
    main()