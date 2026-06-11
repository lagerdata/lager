# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for custom-device surfacing in ``cli/impl/query_instruments.py``.

The script executes on the box, where it consults the custom-device store
(``lager.devices.custom_store``) and emits a synthetic instrument record for
every assigned cable that is currently plugged in — so ``nets add`` and the
TUI see e.g. a Rigol DP711 instead of a generic Prolific USB-serial adapter.

Fully hermetic:

* ``serial`` (pyserial) is stubbed in ``sys.modules`` before the script loads;
* the real ``lager.devices`` modules load via the bare-namespace import trick
  (no ``lager/__init__.py``, whose deps pull the py3.13-removed ``cgi`` chain);
* the store path is redirected to a temp file;
* ``_scan_usb`` / ``_by_handshake`` / ``serial_id.resolve_tty`` are
  monkeypatched — no /sys, no hardware, no tty probing.
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")
IMPL_PATH = os.path.join(REPO_ROOT, "cli", "impl", "query_instruments.py")

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


# Stub pyserial before loading the script (top-level ``from serial import``);
# these tests never open a port.
if "serial" not in sys.modules:
    _serial_stub = types.ModuleType("serial")
    _serial_stub.Serial = object
    _serial_stub.SerialException = Exception
    sys.modules["serial"] = _serial_stub

# Bare ``lager`` / ``lager.devices`` namespaces, then the real pure modules.
_ensure_package("lager", "lager")
_lager_devices = _ensure_package("lager.devices", "lager", "devices")
catalog = _load_module(
    "lager.devices.catalog", os.path.join(BOX_DIR, "lager", "devices", "catalog.py"))
serial_id = _load_module(
    "lager.devices.serial_id", os.path.join(BOX_DIR, "lager", "devices", "serial_id.py"))
cs = _load_module(
    "lager.devices.custom_store",
    os.path.join(BOX_DIR, "lager", "devices", "custom_store.py"))
# ``from lager.devices import catalog`` resolves via the parent module's
# attributes first — set them explicitly rather than relying on the
# sys.modules fallback.
_lager_devices.catalog = catalog
_lager_devices.serial_id = serial_id
_lager_devices.custom_store = cs

qi = _load_module("query_instruments_under_test", IMPL_PATH)


# A real Prolific USB-serial cable identity (the DP711's adapter).
VID, PID, SERIAL = "067b", "23a3", "00000006"
TTY = "/dev/ttyUSB0"
SERIAL_ADDR = f"serial://{VID}:{PID}/serial/{SERIAL}"


def _prolific_entry(serial=SERIAL):
    """A generic uart record as _scan_usb would emit for the bare cable."""
    return {
        "name": "Prolific_USB_Serial",
        "vid": VID,
        "pid": PID,
        "serial": serial,
        "address": f"USB0::0x{VID.upper()}::0x{PID.upper()}::{serial or ''}::INSTR",
        "net_type": ["uart"],
        "channels": {"uart": [TTY]},
        "tty_path": TTY,
        "tty_paths": [TTY],
    }


class QueryInstrumentsCustomTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_store_path = cs.STORE_PATH
        cs.STORE_PATH = os.path.join(self._tmp, "custom_devices.json")

        # Live-cable map consulted by the fake resolve_tty.
        self.resolved = {}
        self._orig_resolve_tty = serial_id.resolve_tty

        def fake_resolve_tty(vid, pid, serial=None, port_path=None):
            return self.resolved.get((vid, pid, serial, port_path))

        serial_id.resolve_tty = fake_resolve_tty

        # Default: nothing on the USB bus; tests override via _set_scan.
        self._orig_scan_usb = qi._scan_usb
        qi._scan_usb = lambda: []

        # Capture the dexarm-probe exclusion set instead of touching ttys.
        self.handshake_excludes = []
        self._orig_by_handshake = qi._by_handshake

        def fake_handshake(*, exclude=None):
            self.handshake_excludes.append(set(exclude or ()))
            return []

        qi._by_handshake = fake_handshake

        self._orig_framework = (qi._catalog, qi._custom_store, qi._serial_id)

    def tearDown(self):
        qi._catalog, qi._custom_store, qi._serial_id = self._orig_framework
        qi._by_handshake = self._orig_by_handshake
        qi._scan_usb = self._orig_scan_usb
        serial_id.resolve_tty = self._orig_resolve_tty
        cs.STORE_PATH = self._orig_store_path
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------

    def _set_scan(self, entries):
        qi._scan_usb = lambda: [dict(e) for e in entries]

    def _run_main(self, argv=()):
        buf = io.StringIO()
        with redirect_stdout(buf):
            qi.main(list(argv))
        return json.loads(buf.getvalue())

    def _assign_live_dp711(self):
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        self.resolved[(VID, PID, SERIAL, None)] = TTY

    # ---- list ------------------------------------------------------------

    def test_assigned_live_cable_surfaces_catalog_instrument(self):
        self._assign_live_dp711()
        self._set_scan([_prolific_entry()])

        out = self._run_main()
        rec = next((d for d in out if d["name"] == "Rigol_DP711"), None)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["address"], SERIAL_ADDR)
        self.assertEqual(rec["net_type"], ["power-supply"])
        self.assertEqual(rec["channels"], {"power-supply": ["1"]})
        self.assertEqual(rec["tty_path"], TTY)
        self.assertTrue(rec["custom"])
        self.assertEqual(rec["vid"], VID)
        self.assertEqual(rec["pid"], PID)
        self.assertEqual(rec["serial"], SERIAL)

    def test_generic_uart_record_suppressed_for_assigned_cable(self):
        self._assign_live_dp711()
        self._set_scan([_prolific_entry()])

        names = [d["name"] for d in self._run_main()]
        self.assertNotIn("Prolific_USB_Serial", names)
        self.assertIn("Rigol_DP711", names)

    def test_unassigned_cable_passes_through_unchanged(self):
        self._set_scan([_prolific_entry()])

        out = self._run_main()
        self.assertEqual([d["name"] for d in out], ["Prolific_USB_Serial"])
        self.assertNotIn("custom", out[0])

    def test_unplugged_assignment_not_surfaced(self):
        # Assignment exists but resolve_tty finds no live cable (e.g. tty
        # driver not yet bound). The generic record must survive untouched.
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        self._set_scan([_prolific_entry()])

        names = [d["name"] for d in self._run_main()]
        self.assertNotIn("Rigol_DP711", names)
        self.assertIn("Prolific_USB_Serial", names)

    def test_port_path_assignment_surfaced_with_port_address(self):
        port = "1-1.2"
        cs.add("Rigol_DP711", VID, PID, port_path=port)
        self.resolved[(VID, PID, None, port)] = TTY
        # Serial-less cable: the generic record carries no USB serial.
        self._set_scan([_prolific_entry(serial=None)])

        out = self._run_main()
        rec = next((d for d in out if d["name"] == "Rigol_DP711"), None)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["address"], f"serial://{VID}:{PID}/port/{port}")
        # Suppression keys on the tty, so it works for port-path records too.
        self.assertNotIn("Prolific_USB_Serial", [d["name"] for d in out])

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

        names = [d["name"] for d in self._run_main()]
        self.assertIn("FTDI_FT2232H", names)
        self.assertIn("Rigol_DP711", names)

    def test_unknown_instrument_in_store_skipped(self):
        # Hand-written store record bypassing add()'s catalog validation.
        with open(cs.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump([{"instrument": "Flux_Capacitor", "vid": VID, "pid": PID,
                        "serial": SERIAL, "port_path": None}], f)
        self.resolved[(VID, PID, SERIAL, None)] = TTY
        self._set_scan([_prolific_entry()])

        names = [d["name"] for d in self._run_main()]
        self.assertNotIn("Flux_Capacitor", names)
        # No valid custom record -> no suppression either.
        self.assertIn("Prolific_USB_Serial", names)

    # ---- handshake-probe protection ---------------------------------------

    def test_custom_tty_joins_handshake_exclusion(self):
        # Even when the USB scan reports nothing (cable VID:PID not in
        # SUPPORTED_USB), the assigned tty must be shielded from the dexarm
        # G-code probe.
        self._assign_live_dp711()
        self._set_scan([])

        self._run_main()
        self.assertTrue(self.handshake_excludes)
        self.assertIn(TTY, self.handshake_excludes[-1])

    # ---- get_instrument ----------------------------------------------------

    def test_get_instrument_resolves_serial_address(self):
        self._assign_live_dp711()
        self._set_scan([_prolific_entry()])

        result = self._run_main(["get_instrument", SERIAL_ADDR])
        self.assertEqual(result.get("name"), "Rigol_DP711")
        self.assertEqual(result.get("address"), SERIAL_ADDR)

    def test_get_instrument_unassigned_serial_address_returns_empty(self):
        self._set_scan([_prolific_entry()])

        result = self._run_main(["get_instrument", SERIAL_ADDR])
        self.assertEqual(result, {})

    # ---- degraded mode -----------------------------------------------------

    def test_degraded_mode_without_custom_framework(self):
        # Old box image: lager.devices missing, import guard left None. The
        # scan must behave exactly as before custom devices existed.
        self._assign_live_dp711()
        self._set_scan([_prolific_entry()])
        qi._catalog = qi._custom_store = qi._serial_id = None

        out = self._run_main()
        self.assertEqual([d["name"] for d in out], ["Prolific_USB_Serial"])


if __name__ == "__main__":
    unittest.main()
