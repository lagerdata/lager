# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the custom-device persistence store.

``lager.devices.custom_store`` maps a USB-serial cable identity (vid/pid +
serial, else port path) to a catalog instrument and persists it as JSON. The
scanner consults it to surface an assigned instrument; the assign flow writes
to it. These tests are fully hermetic: the store path is redirected to a temp
file and no real hardware or ``/etc/lager`` is touched.

The module is imported without executing ``lager/__init__.py`` (which pulls in
heavy deps) by registering bare package namespaces in ``sys.modules`` first and
loading the pure ``catalog`` / ``serial_id`` dependencies directly — the same
trick the other box unit tests use.
"""

import importlib.util
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


# Bare ``lager`` / ``lager.devices`` namespaces (don't run their __init__),
# then the pure dependency modules, then the store under test.
_ensure_package("lager", "lager")
_ensure_package("lager.devices", "lager", "devices")
_load_module("lager.devices.catalog", os.path.join(BOX_DIR, "lager", "devices", "catalog.py"))
_load_module("lager.devices.serial_id", os.path.join(BOX_DIR, "lager", "devices", "serial_id.py"))
cs = _load_module(
    "lager.devices.custom_store",
    os.path.join(BOX_DIR, "lager", "devices", "custom_store.py"),
)

# A real Prolific USB-serial cable identity (the DP711's adapter).
VID, PID = "067b", "23a3"


class CustomStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_path = cs.STORE_PATH
        cs.STORE_PATH = os.path.join(self._tmp, "custom_devices.json")

    def tearDown(self):
        cs.STORE_PATH = self._orig_path
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- add / resolve --------------------------------------------------

    def test_add_and_resolve_by_serial(self):
        rec = cs.add("Rigol_DP711", VID, PID, serial="00000006")
        self.assertEqual(rec["instrument"], "Rigol_DP711")
        self.assertTrue(os.path.exists(cs.STORE_PATH))
        self.assertEqual(cs.resolve(VID, PID, serial="00000006"), "Rigol_DP711")

    def test_add_canonicalizes_instrument_name(self):
        rec = cs.add("rigol_dp711", VID, PID, serial="s")
        self.assertEqual(rec["instrument"], "Rigol_DP711")

    def test_resolve_by_port_when_no_serial(self):
        cs.add("Rigol_DP711", VID, PID, port_path="1-1.2")
        self.assertEqual(cs.resolve(VID, PID, port_path="1-1.2"), "Rigol_DP711")
        # A port-only record must not match a serial lookup.
        self.assertIsNone(cs.resolve(VID, PID, serial="00000006"))

    def test_serial_record_matches_only_on_serial(self):
        cs.add("Rigol_DP711", VID, PID, serial="AAA")
        self.assertIsNone(cs.resolve(VID, PID, serial="BBB"))
        self.assertIsNone(cs.resolve(VID, PID, port_path="1-1"))

    def test_resolve_requires_vid_pid_match(self):
        cs.add("Rigol_DP711", VID, PID, serial="s")
        self.assertIsNone(cs.resolve("ffff", PID, serial="s"))
        self.assertIsNone(cs.resolve(VID, "ffff", serial="s"))

    def test_vid_pid_normalized_case_insensitive(self):
        cs.add("Rigol_DP711", "067B", "23A3", serial="s")
        self.assertEqual(cs.resolve("067b", "23a3", serial="s"), "Rigol_DP711")
        self.assertEqual(cs.resolve("067B", "23A3", serial="s"), "Rigol_DP711")

    # ---- upsert / remove ------------------------------------------------

    def test_add_upserts_same_identity(self):
        cs.add("Rigol_DP711", VID, PID, serial="s")
        cs.add("Rigol_DP711", VID, PID, serial="s")
        self.assertEqual(len(cs.load()), 1)

    def test_remove(self):
        cs.add("Rigol_DP711", VID, PID, serial="s")
        self.assertTrue(cs.remove(VID, PID, serial="s"))
        self.assertEqual(cs.load(), [])
        # Removing again is a no-op that reports nothing removed.
        self.assertFalse(cs.remove(VID, PID, serial="s"))

    # ---- validation -----------------------------------------------------

    def test_add_unknown_instrument_raises(self):
        with self.assertRaises(ValueError):
            cs.add("Not_A_Real_Device", VID, PID, serial="s")

    def test_add_without_identity_raises(self):
        with self.assertRaises(ValueError):
            cs.add("Rigol_DP711", VID, PID)

    # ---- address round-trip --------------------------------------------

    def test_instrument_for_address_roundtrip(self):
        rec = cs.add("Rigol_DP711", VID, PID, serial="00000006")
        addr = cs.address_for(rec)
        self.assertTrue(addr.startswith("serial://067b:23a3/serial/"))
        self.assertEqual(cs.instrument_for_address(addr), "Rigol_DP711")

    def test_instrument_for_address_unknown_returns_none(self):
        self.assertIsNone(cs.instrument_for_address("serial://067b:23a3/serial/nope"))
        self.assertIsNone(cs.instrument_for_address("not-an-address"))

    # ---- baud override / serial settings ---------------------------------

    def test_add_with_baud_persists_override(self):
        rec = cs.add("Rigol_DP711", VID, PID, serial="s", baud=19200)
        self.assertEqual(rec["baud"], 19200)
        self.assertEqual(cs.load()[0]["baud"], 19200)

    def test_add_without_baud_omits_field(self):
        rec = cs.add("Rigol_DP711", VID, PID, serial="s")
        self.assertNotIn("baud", rec)

    def test_upsert_without_baud_drops_old_override(self):
        cs.add("Rigol_DP711", VID, PID, serial="s", baud=19200)
        cs.add("Rigol_DP711", VID, PID, serial="s")
        records = cs.load()
        self.assertEqual(len(records), 1)
        self.assertNotIn("baud", records[0])

    def test_serial_settings_default_to_catalog(self):
        rec = cs.add("Rigol_DP711", VID, PID, serial="00000006")
        settings = cs.serial_settings_for_address(cs.address_for(rec))
        # Catalog defaults (DP700 factory settings) pass through untouched.
        self.assertEqual(settings["baud"], 9600)
        self.assertEqual(settings["parity"], "N")

    def test_serial_settings_apply_baud_override(self):
        rec = cs.add("Rigol_DP711", VID, PID, serial="00000006", baud=19200)
        settings = cs.serial_settings_for_address(cs.address_for(rec))
        self.assertEqual(settings["baud"], 19200)
        # Only baud is overridable; the rest stays catalog-defined.
        self.assertEqual(settings["bytesize"], 8)

    def test_serial_settings_none_for_unassigned_or_invalid_address(self):
        self.assertIsNone(cs.serial_settings_for_address("serial://067b:23a3/serial/nope"))
        self.assertIsNone(cs.serial_settings_for_address("USB0::0x1AB1::0x0E11::X::INSTR"))

    # ---- record_for -------------------------------------------------------

    def test_record_for_returns_full_record(self):
        cs.add("Rigol_DP711", VID, PID, serial="s", baud=19200)
        rec = cs.record_for(VID, PID, serial="s")
        self.assertEqual(rec["instrument"], "Rigol_DP711")
        self.assertEqual(rec["baud"], 19200)
        self.assertIsNone(cs.record_for(VID, PID, serial="other"))

    # ---- resilience -----------------------------------------------------

    def test_corrupt_store_is_tolerated(self):
        with open(cs.STORE_PATH, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json")
        self.assertEqual(cs.load(), [])  # treated as empty, not a crash
        # A subsequent add overwrites the corrupt file cleanly.
        cs.add("Rigol_DP711", VID, PID, serial="s")
        self.assertEqual(cs.resolve(VID, PID, serial="s"), "Rigol_DP711")

    def test_load_missing_file_returns_empty(self):
        self.assertFalse(os.path.exists(cs.STORE_PATH))
        self.assertEqual(cs.load(), [])


if __name__ == "__main__":
    unittest.main()
