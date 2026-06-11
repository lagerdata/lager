# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``cli/impl/custom_devices.py`` — the box-side backend of
``lager nets assign``.

The script's three commands (``list`` / ``assign`` / ``remove``) read and
write the custom-device store and enumerate live cables. Fully hermetic: the
real ``lager.devices`` modules load via the bare-namespace trick, the store
path is redirected to a temp file, and ``serial_id.list_cables`` /
``resolve_tty`` are monkeypatched — no /sys, no hardware.
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
from contextlib import redirect_stderr, redirect_stdout

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")
IMPL_PATH = os.path.join(REPO_ROOT, "cli", "impl", "custom_devices.py")

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

cd = _load_module("custom_devices_under_test", IMPL_PATH)


class _FakeNet:
    """In-memory stand-in for ``lager.nets.net.Net`` (the cascade's import)."""

    db: list = []

    @classmethod
    def get_local_nets(cls):
        return [dict(n) for n in cls.db]

    @classmethod
    def save_local_nets(cls, nets):
        cls.db = [dict(n) for n in nets]


# A real Prolific USB-serial cable identity (the DP711's adapter).
VID, PID, SERIAL = "067b", "23a3", "00000006"
TTY = "/dev/ttyUSB0"
CABLE = {"vid": VID, "pid": PID, "serial": SERIAL, "port_path": "1-1.2", "tty": TTY}
SERIAL_ADDR = f"serial://{VID}:{PID}/serial/{SERIAL}"
PORT_ADDR = f"serial://{VID}:{PID}/port/1-1.2"


class CustomDevicesImplTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_store_path = cs.STORE_PATH
        cs.STORE_PATH = os.path.join(self._tmp, "custom_devices.json")

        # Live cables and identity->tty resolution, both patchable per test.
        self.cables = []
        self.resolved = {}
        self._orig_list_cables = serial_id.list_cables
        self._orig_resolve_tty = serial_id.resolve_tty
        serial_id.list_cables = lambda: [dict(c) for c in self.cables]

        def fake_resolve_tty(vid, pid, serial=None, port_path=None):
            return self.resolved.get((vid, pid, serial, port_path))

        serial_id.resolve_tty = fake_resolve_tty

        self._orig_framework = (cd._catalog, cd._custom_store, cd._serial_id)

        # Fake nets module for the assignment->nets cascade. Registered in
        # sys.modules so the impl's lazy ``from lager.nets.net import Net``
        # never touches the real (heavy) box module.
        _FakeNet.db = []
        self._saved_net_modules = {
            name: sys.modules.get(name) for name in ("lager.nets", "lager.nets.net")
        }
        nets_pkg = types.ModuleType("lager.nets")
        nets_pkg.__path__ = []  # bare namespace; never import from disk
        nets_mod = types.ModuleType("lager.nets.net")
        nets_mod.Net = _FakeNet
        sys.modules["lager.nets"] = nets_pkg
        sys.modules["lager.nets.net"] = nets_mod

    def tearDown(self):
        for name, mod in self._saved_net_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        cd._catalog, cd._custom_store, cd._serial_id = self._orig_framework
        serial_id.resolve_tty = self._orig_resolve_tty
        serial_id.list_cables = self._orig_list_cables
        cs.STORE_PATH = self._orig_store_path
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------

    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            cd.main(list(argv))
        return json.loads(out.getvalue())

    def _run_expect_failure(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(out), redirect_stderr(err):
                cd.main(list(argv))
        self.assertNotEqual(caught.exception.code, 0)
        return err.getvalue()

    # ---- list --------------------------------------------------------------

    def test_list_empty_store_shows_catalog_and_cables(self):
        self.cables = [CABLE]
        data = self._run(["list"])
        names = [e["name"] for e in data["catalog"]]
        self.assertIn("Rigol_DP711", names)
        dp711 = next(e for e in data["catalog"] if e["name"] == "Rigol_DP711")
        self.assertEqual(dp711["default_baud"], 9600)
        self.assertEqual(dp711["roles"], ["power-supply"])
        self.assertEqual(data["assignments"], [])
        self.assertEqual(data["cables"], [CABLE])

    def test_list_shows_assignment_and_hides_assigned_cable(self):
        self.cables = [CABLE]
        self.resolved[(VID, PID, SERIAL, None)] = TTY
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)

        data = self._run(["list"])
        self.assertEqual(len(data["assignments"]), 1)
        a = data["assignments"][0]
        self.assertEqual(a["instrument"], "Rigol_DP711")
        self.assertEqual(a["tty"], TTY)
        self.assertEqual(a["address"], f"serial://{VID}:{PID}/serial/{SERIAL}")
        self.assertEqual(data["cables"], [])

    def test_list_unplugged_assignment_has_null_tty(self):
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        data = self._run(["list"])
        self.assertIsNone(data["assignments"][0]["tty"])

    # ---- assign --------------------------------------------------------------

    def test_assign_by_serial(self):
        self.cables = [CABLE]
        result = self._run(["assign", json.dumps(
            {"instrument": "Rigol_DP711", "serial": SERIAL})])
        self.assertEqual(result["instrument"], "Rigol_DP711")
        self.assertEqual(result["address"], f"serial://{VID}:{PID}/serial/{SERIAL}")
        self.assertEqual(result["tty"], TTY)
        self.assertEqual(result["roles"], ["power-supply"])
        self.assertEqual(result["channels"], {"power-supply": ["1"]})
        # Persisted with the vid/pid captured from the live cable.
        self.assertEqual(cs.resolve(VID, PID, serial=SERIAL), "Rigol_DP711")

    def test_assign_by_port_with_baud(self):
        self.cables = [CABLE]
        result = self._run(["assign", json.dumps(
            {"instrument": "rigol_dp711", "port_path": "1-1.2", "baud": 19200})])
        self.assertEqual(result["instrument"], "Rigol_DP711")  # canonicalized
        self.assertEqual(result["baud"], 19200)
        self.assertEqual(result["address"], f"serial://{VID}:{PID}/port/1-1.2")

    def test_assign_unknown_instrument_fails(self):
        self.cables = [CABLE]
        err = self._run_expect_failure(["assign", json.dumps(
            {"instrument": "Flux_Capacitor", "serial": SERIAL})])
        self.assertIn("Unknown device", err)
        self.assertIn("Rigol_DP711", err)  # lists the assignable devices

    def test_assign_unplugged_cable_fails(self):
        self.cables = []
        err = self._run_expect_failure(["assign", json.dumps(
            {"instrument": "Rigol_DP711", "serial": SERIAL})])
        self.assertIn("currently connected", err)
        self.assertEqual(cs.load(), [])

    def test_assign_requires_exactly_one_identity(self):
        self.cables = [CABLE]
        err = self._run_expect_failure(["assign", json.dumps(
            {"instrument": "Rigol_DP711", "serial": SERIAL, "port_path": "1-1.2"})])
        self.assertIn("exactly one", err)
        err = self._run_expect_failure(["assign", json.dumps(
            {"instrument": "Rigol_DP711"})])
        self.assertIn("exactly one", err)

    def test_assign_ambiguous_serial_fails(self):
        # Two clone cables sharing one (fake) serial — the classic Prolific
        # failure mode; the user must pin by port path instead.
        self.cables = [CABLE, {**CABLE, "port_path": "1-1.3", "tty": "/dev/ttyUSB1"}]
        err = self._run_expect_failure(["assign", json.dumps(
            {"instrument": "Rigol_DP711", "serial": SERIAL})])
        self.assertIn("Multiple", err)
        self.assertIn("port path", err)

    # ---- remove --------------------------------------------------------------

    def test_remove_existing_assignment(self):
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        result = self._run(["remove", json.dumps({"serial": SERIAL})])
        self.assertTrue(result["removed"])
        self.assertEqual(result["instrument"], "Rigol_DP711")
        self.assertEqual(cs.load(), [])

    def test_remove_missing_assignment(self):
        result = self._run(["remove", json.dumps({"serial": "nope"})])
        self.assertEqual(result, {"removed": False})

    def test_remove_by_port_path(self):
        cs.add("Rigol_DP711", VID, PID, port_path="1-1.2")
        result = self._run(["remove", json.dumps({"port_path": "1-1.2"})])
        self.assertTrue(result["removed"])

    # ---- assignment -> nets cascade ---------------------------------------------
    # Nets live and die with their assignment: removing (or replacing) an
    # assignment deletes the saved nets bound to its address. Without the
    # cascade a stale net keeps driving the instrument — the DP700 driver
    # resolves serial:// addresses from sysfs without consulting the store.

    def _add_second_catalog_device(self):
        catalog.DEVICE_CATALOG["Test_PSU"] = {
            "display_name": "Test PSU",
            "roles": ["power-supply"],
            "channels": {"power-supply": ["1"]},
            "single_channel": True,
            "transport": "serial",
            "serial": {"baud": 9600},
        }
        self.addCleanup(catalog.DEVICE_CATALOG.pop, "Test_PSU", None)

    def test_remove_deletes_nets_bound_to_assignment(self):
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        _FakeNet.db = [
            {"name": "supply1", "role": "power-supply", "address": SERIAL_ADDR},
            {"name": "other", "role": "uart", "address": "USB0::0x10C4::0xEA60::X::INSTR"},
        ]

        result = self._run(["remove", json.dumps({"serial": SERIAL})])
        self.assertTrue(result["removed"])
        self.assertEqual(result["deleted_nets"], ["supply1"])
        # Unrelated nets survive untouched.
        self.assertEqual([n["name"] for n in _FakeNet.db], ["other"])

    def test_remove_without_nets_reports_empty_cascade(self):
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        result = self._run(["remove", json.dumps({"serial": SERIAL})])
        self.assertTrue(result["removed"])
        self.assertEqual(result["deleted_nets"], [])

    def test_remove_missing_assignment_touches_no_nets(self):
        _FakeNet.db = [{"name": "supply1", "address": SERIAL_ADDR}]
        result = self._run(["remove", json.dumps({"serial": "nope"})])
        self.assertEqual(result, {"removed": False})
        self.assertEqual(len(_FakeNet.db), 1)

    def test_remove_port_assignment_deletes_port_address_nets(self):
        cs.add("Rigol_DP711", VID, PID, port_path="1-1.2")
        _FakeNet.db = [{"name": "supply1", "address": PORT_ADDR}]
        result = self._run(["remove", json.dumps({"port_path": "1-1.2"})])
        self.assertEqual(result["deleted_nets"], ["supply1"])
        self.assertEqual(_FakeNet.db, [])

    def test_remove_with_cable_unplugged_still_cascades(self):
        # remove is a store-only operation; the cascade must not depend on
        # the cable being live.
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        _FakeNet.db = [{"name": "supply1", "address": SERIAL_ADDR}]
        self.cables = []
        result = self._run(["remove", json.dumps({"serial": SERIAL})])
        self.assertTrue(result["removed"])
        self.assertEqual(result["deleted_nets"], ["supply1"])

    def test_rebaud_same_instrument_keeps_nets(self):
        # A --baud update re-assigns with the same identity + instrument:
        # the address and instrument stand, so the nets must survive.
        self.cables = [CABLE]
        self._run(["assign", json.dumps({"instrument": "Rigol_DP711", "serial": SERIAL})])
        _FakeNet.db = [{"name": "supply1", "address": SERIAL_ADDR}]

        result = self._run(["assign", json.dumps(
            {"instrument": "Rigol_DP711", "serial": SERIAL, "baud": 19200})])
        self.assertEqual(result["deleted_nets"], [])
        self.assertEqual(len(_FakeNet.db), 1)
        self.assertEqual(cs.load()[0]["baud"], 19200)

    def test_reassign_different_instrument_deletes_nets(self):
        self._add_second_catalog_device()
        self.cables = [CABLE]
        self._run(["assign", json.dumps({"instrument": "Rigol_DP711", "serial": SERIAL})])
        _FakeNet.db = [{"name": "supply1", "address": SERIAL_ADDR}]

        result = self._run(["assign", json.dumps(
            {"instrument": "Test_PSU", "serial": SERIAL})])
        self.assertEqual(result["instrument"], "Test_PSU")
        self.assertEqual(result["deleted_nets"], ["supply1"])
        self.assertEqual(_FakeNet.db, [])
        # Still exactly one assignment for the cable (upsert).
        self.assertEqual(len(cs.load()), 1)

    def test_reassign_identity_form_change_replaces_record_and_deletes_nets(self):
        # serial-keyed -> port-keyed: the old address loses its assignment,
        # so its nets go and the old record is dropped (one cable == one
        # assignment, never two records under different identity forms).
        self.cables = [CABLE]
        self._run(["assign", json.dumps({"instrument": "Rigol_DP711", "serial": SERIAL})])
        _FakeNet.db = [{"name": "supply1", "address": SERIAL_ADDR}]

        result = self._run(["assign", json.dumps(
            {"instrument": "Rigol_DP711", "port_path": "1-1.2"})])
        self.assertEqual(result["address"], PORT_ADDR)
        self.assertEqual(result["deleted_nets"], ["supply1"])
        records = cs.load()
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["serial"])
        self.assertEqual(records[0]["port_path"], "1-1.2")

    def test_fresh_assign_deletes_nothing(self):
        self.cables = [CABLE]
        _FakeNet.db = [{"name": "other", "address": "USB0::0x10C4::0xEA60::X::INSTR"}]
        result = self._run(["assign", json.dumps(
            {"instrument": "Rigol_DP711", "serial": SERIAL})])
        self.assertEqual(result["deleted_nets"], [])
        self.assertEqual(len(_FakeNet.db), 1)

    def test_assign_retires_preexisting_uart_nets_on_the_cable(self):
        # A generic UART net saved for the bare cable BEFORE assignment would
        # keep the tty drivable as a terminal while the instrument driver
        # owns it. Assignment retires it (matched by exact USB serial in the
        # VISA address or pin); unrelated uart nets survive.
        self.cables = [CABLE]
        _FakeNet.db = [
            {"name": "uart1", "role": "uart",
             "address": f"USB0::0x067B::0x23A3::{SERIAL}::INSTR", "pin": TTY},
            {"name": "uart2", "role": "uart",
             "address": "USB0::0x10C4::0xEA60::OTHER::INSTR", "pin": "/dev/ttyUSB1"},
            {"name": "dbg", "role": "debug",
             "address": f"USB0::0x067B::0x23A3::{SERIAL}::INSTR", "pin": "X"},
        ]
        result = self._run(["assign", json.dumps(
            {"instrument": "Rigol_DP711", "serial": SERIAL})])
        self.assertEqual(result["deleted_nets"], ["uart1"])
        # Non-uart roles and other cables' uart nets are untouched.
        self.assertEqual(sorted(n["name"] for n in _FakeNet.db), ["dbg", "uart2"])

    def test_assign_retires_uart_net_keyed_by_pin_serial(self):
        # Legacy uart nets sometimes carry the USB serial in the pin field.
        self.cables = [CABLE]
        _FakeNet.db = [{"name": "uart1", "role": "uart",
                        "address": "/dev/ttyUSB0", "pin": SERIAL}]
        result = self._run(["assign", json.dumps(
            {"instrument": "Rigol_DP711", "serial": SERIAL})])
        self.assertEqual(result["deleted_nets"], ["uart1"])

    def test_serial_less_cable_assign_leaves_uart_nets_alone(self):
        # tty paths renumber, so a path match could hit a different cable —
        # the conservative rule skips the uart cascade for port-pinned
        # (serial-less) assignments.
        cable = {**CABLE, "serial": None}
        self.cables = [cable]
        _FakeNet.db = [{"name": "uart1", "role": "uart",
                        "address": "USB0::0x067B::0x23A3::::INSTR", "pin": TTY}]
        result = self._run(["assign", json.dumps(
            {"instrument": "Rigol_DP711", "port_path": "1-1.2"})])
        self.assertEqual(result["deleted_nets"], [])
        self.assertEqual(len(_FakeNet.db), 1)

    def test_cascade_degrades_when_nets_module_unavailable(self):
        # sys.modules[name] = None makes the lazy import raise ImportError:
        # the removal itself must still succeed, just without the cascade.
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        sys.modules["lager.nets.net"] = None
        result = self._run(["remove", json.dumps({"serial": SERIAL})])
        self.assertTrue(result["removed"])
        self.assertEqual(result["deleted_nets"], [])

    # ---- protocol / degraded mode ---------------------------------------------

    def test_invalid_json_payload_fails(self):
        err = self._run_expect_failure(["assign", "{not json"])
        self.assertIn("Invalid JSON", err)

    def test_unknown_command_fails(self):
        err = self._run_expect_failure(["frobnicate"])
        self.assertIn("Unknown command", err)

    def test_degraded_mode_reports_old_box_image(self):
        cd._catalog = cd._custom_store = cd._serial_id = None
        err = self._run_expect_failure(["list"])
        self.assertIn("predates custom serial devices", err)


if __name__ == "__main__":
    unittest.main()
