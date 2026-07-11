# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for custom-device surfacing in the box HTTP scanner.

``lager.http_handlers.usb_scanner.list_instruments`` (served by GET
/instruments/list) mirrors ``cli/impl/query_instruments.py``: live
custom-device assignments are surfaced as synthetic instrument records and
their generic UART cable records suppressed. Fully hermetic — the store path
is redirected, and ``scan_usb`` / ``_by_handshake`` / ``serial_id.resolve_tty``
are monkeypatched (no /sys, no hardware, no tty probing). Modules load via the
bare-namespace import trick (no ``lager/__init__.py``).
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

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
_lager_devices = _ensure_package("lager.devices", "lager", "devices")
_ensure_package("lager.http_handlers", "lager", "http_handlers")
catalog = _load_module(
    "lager.devices.catalog", os.path.join(BOX_DIR, "lager", "devices", "catalog.py"))
serial_id = _load_module(
    "lager.devices.serial_id", os.path.join(BOX_DIR, "lager", "devices", "serial_id.py"))
cs = _load_module(
    "lager.devices.custom_store",
    os.path.join(BOX_DIR, "lager", "devices", "custom_store.py"))
_lager_devices.catalog = catalog
_lager_devices.serial_id = serial_id
_lager_devices.custom_store = cs

us = _load_module(
    "lager.http_handlers.usb_scanner",
    os.path.join(BOX_DIR, "lager", "http_handlers", "usb_scanner.py"))


# A real Prolific USB-serial cable identity (the DP711's adapter).
VID, PID, SERIAL = "067b", "23a3", "00000006"
TTY = "/dev/ttyUSB0"
SERIAL_ADDR = f"serial://{VID}:{PID}/serial/{SERIAL}"


def _prolific_entry():
    """A generic uart record as scan_usb would emit for the bare cable."""
    return {
        "name": "Prolific_USB_Serial",
        "vid": VID,
        "pid": PID,
        "serial": SERIAL,
        "address": f"USB0::0x{VID.upper()}::0x{PID.upper()}::{SERIAL}::INSTR",
        "net_type": ["uart"],
        "channels": {"uart": [TTY]},
        "tty_path": TTY,
        "tty_paths": [TTY],
    }


class UsbScannerCustomTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_store_path = cs.STORE_PATH
        cs.STORE_PATH = os.path.join(self._tmp, "custom_devices.json")

        self.resolved = {}
        self._orig_resolve_tty = serial_id.resolve_tty

        def fake_resolve_tty(vid, pid, serial=None, port_path=None):
            return self.resolved.get((vid, pid, serial, port_path))

        serial_id.resolve_tty = fake_resolve_tty

        self._orig_scan_usb = us.scan_usb
        us.scan_usb = lambda: []

        self.handshake_excludes = []
        self._orig_by_handshake = us._by_handshake

        def fake_handshake(*, exclude=None):
            self.handshake_excludes.append(set(exclude or ()))
            return []

        us._by_handshake = fake_handshake

        self._orig_framework = (us._catalog, us._custom_store, us._serial_id)

    def tearDown(self):
        us._catalog, us._custom_store, us._serial_id = self._orig_framework
        us._by_handshake = self._orig_by_handshake
        us.scan_usb = self._orig_scan_usb
        serial_id.resolve_tty = self._orig_resolve_tty
        cs.STORE_PATH = self._orig_store_path
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------

    def _set_scan(self, entries):
        us.scan_usb = lambda: [dict(e) for e in entries]

    def _assign_live_dp711(self):
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        self.resolved[(VID, PID, SERIAL, None)] = TTY

    # ---- tests -----------------------------------------------------------

    def test_assigned_live_cable_surfaces_catalog_instrument(self):
        self._assign_live_dp711()
        self._set_scan([_prolific_entry()])

        out = us.list_instruments()
        rec = next((d for d in out if d["name"] == "Rigol_DP711"), None)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["address"], SERIAL_ADDR)
        self.assertEqual(rec["net_type"], ["power-supply"])
        self.assertEqual(rec["channels"], {"power-supply": ["1"]})
        self.assertEqual(rec["tty_path"], TTY)
        self.assertTrue(rec["custom"])
        # The generic cable record is replaced, not duplicated.
        self.assertNotIn("Prolific_USB_Serial", [d["name"] for d in out])
        # JSON-serializable for the /instruments/list HTTP response.
        json.dumps(out)

    def test_unassigned_cable_passes_through_unchanged(self):
        self._set_scan([_prolific_entry()])

        out = us.list_instruments()
        self.assertEqual([d["name"] for d in out], ["Prolific_USB_Serial"])
        self.assertNotIn("custom", out[0])

    def test_unplugged_assignment_not_surfaced(self):
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        self._set_scan([_prolific_entry()])

        names = [d["name"] for d in us.list_instruments()]
        self.assertNotIn("Rigol_DP711", names)
        self.assertIn("Prolific_USB_Serial", names)

    def test_multi_role_chip_not_suppressed(self):
        # An FTDI debug+uart chip sharing the custom tty keeps its generic
        # record — only UART-only entries are replaced.
        self._assign_live_dp711()
        ftdi = {
            "name": "FTDI_FT2232H",
            "vid": "0403", "pid": "6010", "serial": "FT123",
            "address": "USB0::0x0403::0x6010::FT123::INSTR",
            "net_type": ["spi", "i2c", "gpio", "debug", "uart"],
            "channels": {"uart": [TTY]},
            "tty_path": TTY,
            "tty_paths": [TTY],
        }
        self._set_scan([ftdi])

        names = [d["name"] for d in us.list_instruments()]
        self.assertIn("FTDI_FT2232H", names)
        self.assertIn("Rigol_DP711", names)

    def test_unknown_instrument_in_store_skipped(self):
        # Hand-written store record bypassing add()'s catalog validation.
        with open(cs.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump([{"instrument": "Flux_Capacitor", "vid": VID, "pid": PID,
                        "serial": SERIAL, "port_path": None}], f)
        self.resolved[(VID, PID, SERIAL, None)] = TTY
        self._set_scan([_prolific_entry()])

        names = [d["name"] for d in us.list_instruments()]
        self.assertNotIn("Flux_Capacitor", names)
        # No valid custom record -> no suppression either.
        self.assertIn("Prolific_USB_Serial", names)

    def test_port_path_assignment_surfaced_with_port_address(self):
        port = "1-1.2"
        cs.add("Rigol_DP711", VID, PID, port_path=port)
        self.resolved[(VID, PID, None, port)] = TTY
        self._set_scan([])

        out = us.list_instruments()
        self.assertEqual([d["name"] for d in out], ["Rigol_DP711"])
        self.assertEqual(out[0]["address"], f"serial://{VID}:{PID}/port/{port}")

    def test_custom_tty_joins_handshake_exclusion(self):
        self._assign_live_dp711()
        self._set_scan([])

        us.list_instruments()
        self.assertTrue(self.handshake_excludes)
        self.assertIn(TTY, self.handshake_excludes[-1])

    def test_degraded_mode_without_custom_framework(self):
        # Partial deployment without lager.devices: import guard left None —
        # the scanner must behave exactly as before custom devices existed.
        self._assign_live_dp711()
        self._set_scan([_prolific_entry()])
        us._catalog = us._custom_store = us._serial_id = None

        out = us.list_instruments()
        self.assertEqual([d["name"] for d in out], ["Prolific_USB_Serial"])


if __name__ == "__main__":
    unittest.main()
