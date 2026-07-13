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

    def _add_cable(self, tty_name, port, vid=VID, pid=PID, serial=SERIAL, iface=0):
        """Create a USB device dir + interface dir + tty pointing at it."""
        usb_dev = self._devices / port
        usb_dev.mkdir(parents=True, exist_ok=True)
        (usb_dev / "idVendor").write_text(f"{vid}\n")
        (usb_dev / "idProduct").write_text(f"{pid}\n")
        if serial is not None:
            (usb_dev / "serial").write_text(f"{serial}\n")
        iface_dir = usb_dev / f"{port}:1.{iface}"
        iface_dir.mkdir(exist_ok=True)

        tty_dir = self._sys_tty / tty_name
        tty_dir.mkdir()
        (tty_dir / "device").symlink_to(iface_dir)

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

    # ---- identity_for_tty (UART net re-enumeration snapshot) ----------------

    def test_identity_for_tty_fields(self):
        self._add_cable("ttyUSB0", "1-1.2")
        self.assertEqual(serial_id.identity_for_tty("/dev/ttyUSB0"), {
            "vid": VID,
            "pid": PID,
            "serial": SERIAL,
            "port_path": "1-1.2",
            "interface": 0,
        })

    def test_identity_for_tty_parses_interface(self):
        self._add_cable("ttyUSB3", "1-1.5", serial=None, iface=3)
        ident = serial_id.identity_for_tty("/dev/ttyUSB3")
        self.assertEqual(ident["interface"], 3)
        self.assertIsNone(ident["serial"])

    def test_identity_for_tty_follows_symlink(self):
        # /dev/serial/by-id/... style symlink resolves to the tty name.
        self._add_cable("ttyUSB0", "1-1.2")
        link = Path(self._tmp) / "usb-FTDI_cable-if00-port0"
        link.symlink_to("/dev/ttyUSB0")
        ident = serial_id.identity_for_tty(str(link))
        self.assertEqual(ident["port_path"], "1-1.2")

    def test_identity_for_tty_unknown_or_garbage(self):
        self.assertIsNone(serial_id.identity_for_tty("/dev/ttyUSB9"))
        self.assertIsNone(serial_id.identity_for_tty(""))
        self.assertIsNone(serial_id.identity_for_tty(None))

    # ---- resolve_identity ----------------------------------------------------

    def _reenumerate(self, old_tty, new_tty, port, **kwargs):
        """Simulate a re-enumeration: same USB device, new tty number."""
        shutil.rmtree(self._sys_tty / old_tty)
        self._add_cable(new_tty, port, **kwargs)

    def test_resolve_identity_by_serial_survives_renumbering(self):
        self._add_cable("ttyUSB0", "1-1.2")
        ident = serial_id.identity_for_tty("/dev/ttyUSB0")
        self._reenumerate("ttyUSB0", "ttyUSB5", "1-1.2")
        self.assertEqual(serial_id.resolve_identity(ident), "/dev/ttyUSB5")

    def test_resolve_identity_serial_follows_cable_across_ports(self):
        # Serial is the primary key: port_path only breaks ties.
        self._add_cable("ttyUSB0", "1-1.2")
        ident = serial_id.identity_for_tty("/dev/ttyUSB0")
        self._reenumerate("ttyUSB0", "ttyUSB1", "1-1.9")
        self.assertEqual(serial_id.resolve_identity(ident), "/dev/ttyUSB1")

    def test_resolve_identity_clone_serials_port_tiebreak(self):
        self._add_cable("ttyUSB0", "1-1.2", serial="DUP")
        self._add_cable("ttyUSB1", "1-1.3", serial="DUP")
        ident = serial_id.identity_for_tty("/dev/ttyUSB1")
        self.assertEqual(serial_id.resolve_identity(ident), "/dev/ttyUSB1")

    # ---- clone-serial regression (v0.31.5 reconnected to a sibling) ---------

    def test_identity_for_tty_demotes_clone_serial(self):
        # Two live devices share vid/pid/serial: the serial is not identity.
        self._add_cable("ttyUSB0", "1-1.2", serial="0001")
        self._add_cable("ttyUSB1", "1-1.3", serial="0001")
        ident = serial_id.identity_for_tty("/dev/ttyUSB0")
        self.assertIsNone(ident["serial"])
        self.assertEqual(ident["port_path"], "1-1.2")

    def test_identity_for_tty_keeps_unique_serial(self):
        self._add_cable("ttyUSB0", "1-1.2", serial="UNIQ-A")
        self._add_cable("ttyUSB1", "1-1.3", serial="UNIQ-B")
        self.assertEqual(serial_id.identity_for_tty("/dev/ttyUSB0")["serial"],
                         "UNIQ-A")

    def test_identity_for_tty_multi_interface_keeps_serial(self):
        # Four ttys of ONE multi-port chip share the device dir; that is not
        # a clone serial.
        for n in range(4):
            self._add_cable(f"ttyUSB{n}", "1-1.3", serial="QUAD", iface=n)
        self.assertEqual(serial_id.identity_for_tty("/dev/ttyUSB2")["serial"],
                         "QUAD")

    def test_resolve_identity_clone_absent_never_matches_sibling(self):
        # The exact v0.31.5 field failure: a legacy snapshot carrying the
        # clone serial, resolved while the true device is off the bus, must
        # return None (keep retrying) — NOT a look-alike sibling.
        self._add_cable("ttyUSB0", "1-1.2", serial="0001")
        self._add_cable("ttyUSB1", "1-1.3", serial="0001")
        self._add_cable("ttyUSB2", "1-1.4", serial="0001")
        legacy_ident = {"vid": VID, "pid": PID, "serial": "0001",
                        "port_path": "1-1.4", "interface": 0}
        shutil.rmtree(self._sys_tty / "ttyUSB2")          # our device drops
        self.assertIsNone(serial_id.resolve_identity(legacy_ident))
        self._add_cable("ttyUSB5", "1-1.4", serial="0001")  # it returns
        self.assertEqual(serial_id.resolve_identity(legacy_ident), "/dev/ttyUSB5")

    def test_resolve_identity_clone_serial_without_port_is_unresolvable(self):
        self._add_cable("ttyUSB0", "1-1.2", serial="0001")
        self._add_cable("ttyUSB1", "1-1.3", serial="0001")
        ident = {"vid": VID, "pid": PID, "serial": "0001"}
        self.assertIsNone(serial_id.resolve_identity(ident))

    def test_reconnect_snapshot_cycle_with_clones(self):
        # End-to-end shape of the JUL-16 heal: snapshot (serial demoted),
        # device drops (None while absent), returns renumbered on the same
        # port (resolved by port).
        self._add_cable("ttyUSB0", "1-1.2", serial="0001")
        self._add_cable("ttyUSB1", "1-1.3", serial="0001")
        ident = serial_id.identity_for_tty("/dev/ttyUSB0")
        shutil.rmtree(self._sys_tty / "ttyUSB0")
        self.assertIsNone(serial_id.resolve_identity(ident))
        self._add_cable("ttyUSB4", "1-1.2", serial="0001")
        self.assertEqual(serial_id.resolve_identity(ident), "/dev/ttyUSB4")

    def test_resolve_identity_multi_interface_picks_channel(self):
        # FT4232H: one USB device (no serial), four ttys on interfaces 0-3.
        for n in range(4):
            self._add_cable(f"ttyUSB{n}", "1-1.3", serial=None, iface=n)
        ident = serial_id.identity_for_tty("/dev/ttyUSB2")
        self.assertEqual(ident["interface"], 2)
        self._reenumerate("ttyUSB2", "ttyUSB7", "1-1.3", serial=None, iface=2)
        self.assertEqual(serial_id.resolve_identity(ident), "/dev/ttyUSB7")

    def test_resolve_identity_serialless_requires_port_match(self):
        self._add_cable("ttyUSB0", "1-1.4", serial=None)
        ident = serial_id.identity_for_tty("/dev/ttyUSB0")
        self.assertEqual(serial_id.resolve_identity(ident), "/dev/ttyUSB0")
        ident["port_path"] = "1-1.9"
        self.assertIsNone(serial_id.resolve_identity(ident))

    def test_resolve_identity_unplugged_returns_none(self):
        self._add_cable("ttyUSB0", "1-1.2")
        ident = serial_id.identity_for_tty("/dev/ttyUSB0")
        shutil.rmtree(self._sys_tty / "ttyUSB0")
        self.assertIsNone(serial_id.resolve_identity(ident))

    def test_resolve_identity_garbage_input(self):
        self.assertIsNone(serial_id.resolve_identity(None))
        self.assertIsNone(serial_id.resolve_identity("not-a-dict"))
        self.assertIsNone(serial_id.resolve_identity({}))
        self.assertIsNone(serial_id.resolve_identity({"vid": VID}))
        # Non-string vid/pid (hand-edited JSON) must not raise
        self.assertIsNone(serial_id.resolve_identity({"vid": 1234, "pid": 5678}))
        self.assertIsNone(serial_id.resolve_identity({"vid": None, "pid": PID}))

    def test_resolve_identity_unknown_interface_not_excluding(self):
        # A tty whose interface cannot be determined must still match a
        # snapshot that recorded one (single-interface common case).
        usb_dev = self._devices / "1-1.6"
        usb_dev.mkdir(parents=True)
        (usb_dev / "idVendor").write_text(f"{VID}\n")
        (usb_dev / "idProduct").write_text(f"{PID}\n")
        (usb_dev / "serial").write_text("NOIFACE\n")
        tty_dir = self._sys_tty / "ttyACM0"
        tty_dir.mkdir()
        (tty_dir / "device").symlink_to(usb_dev)  # no :cfg.N ancestor
        ident = {"vid": VID, "pid": PID, "serial": "NOIFACE",
                 "port_path": "1-1.6", "interface": 0}
        self.assertEqual(serial_id.resolve_identity(ident), "/dev/ttyACM0")


if __name__ == "__main__":
    unittest.main()
