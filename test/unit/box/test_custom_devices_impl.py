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


# A real Prolific USB-serial cable identity (the DP711's adapter).
VID, PID, SERIAL = "067b", "23a3", "00000006"
TTY = "/dev/ttyUSB0"
CABLE = {"vid": VID, "pid": PID, "serial": SERIAL, "port_path": "1-1.2", "tty": TTY}


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

    def tearDown(self):
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
