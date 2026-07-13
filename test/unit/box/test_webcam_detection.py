# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for sysfs-based webcam detection (``_by_camera``).

Covers both copies of the scanner — ``cli/impl/query_instruments.py`` and
``box/lager/http_handlers/usb_scanner.py`` — against a fake sysfs tree:

* the /dev/videoN node is resolved through the videoN → USB-interface →
  USB-device sysfs chain, not by index arithmetic, so cameras exposing
  different node counts (C920: two, BRIO: four) map correctly side-by-side;
* only the first (capture) node per camera is reported;
* the webcam VID:PID set is derived from SUPPORTED_USB, so every catalog
  entry with a ``webcam`` net_type — including the Logi 4K Pro (046d:087f) —
  is detected without touching the detection code;
* both catalogs advertise the same webcam set.

Fully hermetic: ``serial`` (pyserial) is stubbed before the CLI script loads,
and ``_by_camera`` reads from a temp-dir sysfs stand-in via its ``v4l_root``
parameter.
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CLI_IMPL_PATH = os.path.join(REPO_ROOT, "cli", "impl", "query_instruments.py")
BOX_SCANNER_PATH = os.path.join(REPO_ROOT, "box", "lager", "http_handlers", "usb_scanner.py")

# Stub pyserial before loading the CLI script (top-level ``from serial import``).
if "serial" not in sys.modules:
    _serial_stub = types.ModuleType("serial")
    _serial_stub.Serial = object
    _serial_stub.SerialException = Exception
    sys.modules["serial"] = _serial_stub


def _load_module(dotted, filepath):
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


qi = _load_module("qi_webcam_test", CLI_IMPL_PATH)
box_scanner = _load_module("box_usb_scanner_webcam_test", BOX_SCANNER_PATH)


class FakeSysfs:
    """Builds a /sys stand-in: USB device dirs plus a video4linux dir."""

    def __init__(self, root: Path):
        self.root = root
        self.usb = root / "usb"
        self.v4l = root / "v4l"
        self.usb.mkdir()
        self.v4l.mkdir()
        self._next_node = 0

    def add_camera(self, busport, vid, pid, serial=None, nodes=1):
        """Add a USB camera exposing *nodes* consecutive /dev/videoN nodes."""
        dev = self.usb / busport
        iface = dev / f"{busport}:1.0"
        iface.mkdir(parents=True)
        (dev / "idVendor").write_text(f"{vid}\n")
        (dev / "idProduct").write_text(f"{pid}\n")
        if serial is not None:
            (dev / "serial").write_text(f"{serial}\n")
        first = self._next_node
        for _ in range(nodes):
            node = self.v4l / f"video{self._next_node}"
            node.mkdir()
            (node / "device").symlink_to(iface)
            self._next_node += 1
        return first


class ByCameraTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sysfs = FakeSysfs(Path(self._tmp.name))

    def _run(self, mod):
        return mod._by_camera(v4l_root=self.sysfs.v4l)

    def test_4k_pro_detected(self):
        self.sysfs.add_camera("2-3", "046d", "087f", serial="ABC123", nodes=4)
        for mod in (qi, box_scanner):
            cams = self._run(mod)
            self.assertEqual(len(cams), 1)
            cam = cams[0]
            self.assertEqual(cam["name"], "Logitech_4K_Pro")
            self.assertEqual(cam["vid"], "046d")
            self.assertEqual(cam["pid"], "087f")
            self.assertEqual(cam["serial"], "ABC123")
            self.assertEqual(cam["net_type"], ["webcam"])
            self.assertEqual(cam["channels"], {"webcam": ["/dev/video0"]})
            self.assertEqual(cam["address"], "USB0::0x046D::0x087F::ABC123::INSTR")

    def test_mixed_node_counts_map_correctly(self):
        # A C920 exposes two nodes, a BRIO four. The old idx*4 heuristic
        # would have pointed the BRIO at /dev/video4 (nonexistent here).
        self.sysfs.add_camera("1-1", "046d", "082d", serial="C920SER", nodes=2)
        self.sysfs.add_camera("1-2", "046d", "085e", serial="BRIOSER", nodes=4)
        for mod in (qi, box_scanner):
            cams = self._run(mod)
            by_name = {c["name"]: c for c in cams}
            self.assertEqual(
                set(by_name), {"Logitech_C920", "Logitech_BRIO_HD"})
            self.assertEqual(
                by_name["Logitech_C920"]["channels"], {"webcam": ["/dev/video0"]})
            self.assertEqual(
                by_name["Logitech_BRIO_HD"]["channels"], {"webcam": ["/dev/video2"]})

    def test_unknown_video_device_ignored(self):
        # A capture card / unknown camera should not be reported.
        self.sysfs.add_camera("3-1", "dead", "beef", nodes=2)
        for mod in (qi, box_scanner):
            self.assertEqual(self._run(mod), [])

    def test_no_serial_camera(self):
        self.sysfs.add_camera("4-1", "046d", "0825", nodes=2)  # C270, no serial
        for mod in (qi, box_scanner):
            cams = self._run(mod)
            self.assertEqual(len(cams), 1)
            self.assertIsNone(cams[0]["serial"])
            self.assertEqual(
                cams[0]["address"], "USB0::0x046D::0x0825::::INSTR")

    def test_missing_v4l_root(self):
        for mod in (qi, box_scanner):
            self.assertEqual(
                mod._by_camera(v4l_root=self.sysfs.root / "nope"), [])

    def test_every_catalog_webcam_is_detectable(self):
        for mod in (qi, box_scanner):
            webcams = {
                name: meta for name, meta in mod.SUPPORTED_USB.items()
                if "webcam" in meta["net_type"]
            }
            self.assertIn("Logitech_4K_Pro", webcams)
            self.assertEqual(
                {(m["vid"], m["pid"]) for m in webcams.values()},
                mod._WEBCAM_VIDPIDS,
            )

    def test_catalogs_advertise_same_webcams(self):
        def webcam_entries(mod):
            return {
                name: (meta["vid"], meta["pid"])
                for name, meta in mod.SUPPORTED_USB.items()
                if "webcam" in meta["net_type"]
            }
        self.assertEqual(webcam_entries(qi), webcam_entries(box_scanner))


if __name__ == "__main__":
    unittest.main()
