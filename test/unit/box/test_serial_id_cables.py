# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``lager.devices.serial_id`` tty enumeration and resolution.

``list_cables()`` is the cable picker's data source (assign CLI / TUI);
``resolve_tty()`` turns a stored cable identity back into the live
``/dev/tty*``. Both walk ``/sys/class/tty`` — these tests point the module at
a fake sysfs tree built in a temp dir, so they are fully hermetic. The fake
tree mirrors the real layout: a tty dir whose ``device`` symlink targets a
USB *interface* dir (``1-1.2:1.0``) nested under the USB *device* dir
(``1-1.2``) that carries ``idVendor`` / ``idProduct`` / ``serial``.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)


def _ensure_package(dotted, *parts):
    if dotted in sys.modules:
        return sys.modules[dotted]
    mod = types.ModuleType(dotted)
    mod.__path__ = [os.path.join(BOX_DIR, *parts)]
    mod.__package__ = dotted
    sys.modules[dotted] = mod
    return mod


def _load_module(dotted, filepath):
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


_ensure_package("lager", "lager")
_ensure_package("lager.devices", "lager", "devices")
serial_id = _load_module(
    "lager.devices.serial_id",
    os.path.join(BOX_DIR, "lager", "devices", "serial_id.py"),
)

VID, PID, SERIAL = "067b", "23a3", "00000006"


class SerialIdCablesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._sys_tty = Path(self._tmp) / "sys_tty"
        self._devices = Path(self._tmp) / "devices"
        self._sys_tty.mkdir()
        self._devices.mkdir()
        self._orig_sys_tty = serial_id._SYS_TTY
        serial_id._SYS_TTY = self._sys_tty

    def tearDown(self):
        serial_id._SYS_TTY = self._orig_sys_tty
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- fake sysfs builders ----------------------------------------------

    def _add_cable(self, tty_name, port, vid=VID, pid=PID, serial=SERIAL):
        """Create a USB device dir + interface dir + tty pointing at it."""
        usb_dev = self._devices / port
        usb_dev.mkdir(parents=True, exist_ok=True)
        (usb_dev / "idVendor").write_text(f"{vid}\n")
        (usb_dev / "idProduct").write_text(f"{pid}\n")
        if serial is not None:
            (usb_dev / "serial").write_text(f"{serial}\n")
        iface = usb_dev / f"{port}:1.0"
        iface.mkdir(exist_ok=True)

        tty_dir = self._sys_tty / tty_name
        tty_dir.mkdir()
        (tty_dir / "device").symlink_to(iface)

    # ---- list_cables --------------------------------------------------------

    def test_list_cables_basic(self):
        self._add_cable("ttyUSB0", "1-1.2")
        cables = serial_id.list_cables()
        self.assertEqual(cables, [{
            "vid": VID,
            "pid": PID,
            "serial": SERIAL,
            "port_path": "1-1.2",
            "tty": "/dev/ttyUSB0",
        }])

    def test_list_cables_lowercases_vid_pid(self):
        self._add_cable("ttyUSB0", "1-1.2", vid="067B", pid="23A3")
        cables = serial_id.list_cables()
        self.assertEqual((cables[0]["vid"], cables[0]["pid"]), (VID, PID))

    def test_list_cables_serial_less_cable(self):
        self._add_cable("ttyUSB0", "1-1.4", serial=None)
        cables = serial_id.list_cables()
        self.assertIsNone(cables[0]["serial"])
        self.assertEqual(cables[0]["port_path"], "1-1.4")

    def test_list_cables_multiple_sorted_by_tty(self):
        self._add_cable("ttyUSB1", "1-1.3", serial="B")
        self._add_cable("ttyUSB0", "1-1.2", serial="A")
        ttys = [c["tty"] for c in serial_id.list_cables()]
        self.assertEqual(ttys, ["/dev/ttyUSB0", "/dev/ttyUSB1"])

    def test_list_cables_skips_non_usb_ttys(self):
        self._add_cable("ttyUSB0", "1-1.2")
        (self._sys_tty / "ttyS0").mkdir()           # platform UART: name filter
        (self._sys_tty / "ttyUSB9").mkdir()         # no device link: skipped
        self.assertEqual(len(serial_id.list_cables()), 1)

    def test_list_cables_missing_sysfs_returns_empty(self):
        serial_id._SYS_TTY = Path(self._tmp) / "does-not-exist"
        self.assertEqual(serial_id.list_cables(), [])

    # ---- resolve_tty (back-fills coverage for the Phase-1 resolver) --------

    def test_resolve_tty_by_serial(self):
        self._add_cable("ttyUSB0", "1-1.2")
        self.assertEqual(serial_id.resolve_tty(VID, PID, serial=SERIAL), "/dev/ttyUSB0")
        self.assertIsNone(serial_id.resolve_tty(VID, PID, serial="other"))

    def test_resolve_tty_by_port_path(self):
        self._add_cable("ttyUSB0", "1-1.2", serial=None)
        self.assertEqual(serial_id.resolve_tty(VID, PID, port_path="1-1.2"), "/dev/ttyUSB0")
        self.assertIsNone(serial_id.resolve_tty(VID, PID, port_path="1-1.9"))

    def test_resolve_tty_requires_vid_pid_match(self):
        self._add_cable("ttyUSB0", "1-1.2")
        self.assertIsNone(serial_id.resolve_tty("ffff", PID, serial=SERIAL))

    def test_resolve_address_to_tty_roundtrip(self):
        self._add_cable("ttyUSB0", "1-1.2")
        addr = serial_id.make_address(VID, PID, serial=SERIAL)
        self.assertEqual(serial_id.resolve_address_to_tty(addr), "/dev/ttyUSB0")


if __name__ == "__main__":
    unittest.main()
