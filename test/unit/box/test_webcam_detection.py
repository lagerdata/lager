# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for sysfs-based webcam detection (``_by_camera``).

Covers the box scanner — ``box/lager/http_handlers/usb_scanner.py`` — against
a fake sysfs tree (the CLI copy, ``cli/impl/query_instruments.py``, was
removed by the :9000 migration; the box scanner is now the only catalog):

* the /dev/videoN node is resolved through the videoN → USB-interface →
  USB-device sysfs chain, not by index arithmetic, so cameras exposing
  different node counts (C920: two, BRIO: four) map correctly side-by-side;
* only the first (capture) node per camera is reported;
* any USB video class camera is detected, and SUPPORTED_USB only names it;
* HDMI capture cards are never cameras, though they are UVC too;
* a camera with no serial of its own is addressed by USB port, and a net finds
  its camera again from its address when /dev/videoN numbers move.

Fully hermetic: ``_by_camera`` reads from a temp-dir sysfs stand-in via its
``v4l_root`` parameter.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lager.devices import video_id

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_SCANNER_PATH = os.path.join(REPO_ROOT, "box", "lager", "http_handlers", "usb_scanner.py")


def _load_module(dotted, filepath):
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


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

    def add_camera(self, busport, vid, pid, serial=None, nodes=1, product=None,
                   interface_class="0e"):
        """Add a USB camera exposing *nodes* consecutive /dev/videoN nodes."""
        dev = self.usb / busport
        iface = dev / f"{busport}:1.0"
        iface.mkdir(parents=True)
        (dev / "idVendor").write_text(f"{vid}\n")
        (dev / "idProduct").write_text(f"{pid}\n")
        if serial is not None:
            (dev / "serial").write_text(f"{serial}\n")
        if product is not None:
            (dev / "product").write_text(f"{product}\n")
        (iface / "bInterfaceClass").write_text(f"{interface_class}\n")
        first = self._next_node
        for index in range(nodes):
            node = self.v4l / f"video{self._next_node}"
            node.mkdir()
            (node / "device").symlink_to(iface)
            (node / "index").write_text(f"{index}\n")
            self._next_node += 1
        return first

    def add_platform_node(self):
        """Add a video node with no USB device behind it."""
        parent = self.root / "platform" / "codec"
        parent.mkdir(parents=True)
        node = self.v4l / f"video{self._next_node}"
        node.mkdir()
        (node / "device").symlink_to(parent)
        self._next_node += 1


class ByCameraTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sysfs = FakeSysfs(Path(self._tmp.name))

    def _run(self, mod):
        return mod._by_camera(v4l_root=self.sysfs.v4l)

    def test_4k_pro_detected(self):
        self.sysfs.add_camera("2-3", "046d", "087f", serial="ABC123", nodes=4)
        cams = self._run(box_scanner)
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
        cams = self._run(box_scanner)
        by_name = {c["name"]: c for c in cams}
        self.assertEqual(
            set(by_name), {"Logitech_C920", "Logitech_BRIO_HD"})
        self.assertEqual(
            by_name["Logitech_C920"]["channels"], {"webcam": ["/dev/video0"]})
        self.assertEqual(
            by_name["Logitech_BRIO_HD"]["channels"], {"webcam": ["/dev/video2"]})

    def test_unlisted_uvc_camera_is_detected(self):
        self.sysfs.add_camera("3-1", "dead", "beef", serial="S1", nodes=2,
                              product="Bench Cam")
        cams = self._run(box_scanner)
        self.assertEqual(len(cams), 1)
        self.assertEqual(cams[0]["name"], box_scanner.GENERIC_WEBCAM_NAME)
        self.assertEqual(cams[0]["product"], "Bench Cam")
        self.assertEqual(cams[0]["net_type"], ["webcam"])
        self.assertEqual(cams[0]["channels"], {"webcam": ["/dev/video0"]})
        self.assertEqual(cams[0]["address"], "USB0::0xDEAD::0xBEEF::S1::INSTR")

    def test_a_node_that_is_not_usb_video_class_is_ignored(self):
        self.sysfs.add_camera("3-1", "dead", "beef", nodes=2, interface_class="ff")
        self.assertEqual(self._run(box_scanner), [])

    def test_a_catalog_camera_needs_no_video_class(self):
        self.sysfs.add_camera("3-1", "046d", "082d", serial="S1", interface_class="ff")
        self.assertEqual([c["name"] for c in self._run(box_scanner)], ["Logitech_C920"])

    def test_a_node_with_no_usb_device_is_ignored(self):
        self.sysfs.add_platform_node()
        self.assertEqual(self._run(box_scanner), [])

    def test_a_card_is_never_a_camera_though_it_is_uvc(self):
        for vid, pid in box_scanner.CAPTURE_CARDS:
            self.sysfs.add_camera(f"5-{vid}", vid, pid, nodes=2)
        self.assertEqual(self._run(box_scanner), [])

    def test_a_metadata_node_is_never_the_capture_node(self):
        # A camera whose lowest-numbered node is not its capture stream.
        self.sysfs.add_camera("3-1", "dead", "beef", serial="S1", nodes=2)
        (self.sysfs.v4l / "video0" / "index").write_text("1\n")
        (self.sysfs.v4l / "video1" / "index").write_text("0\n")
        self.assertEqual(self._run(box_scanner)[0]["channels"],
                         {"webcam": ["/dev/video1"]})

    def test_no_serial_camera(self):
        self.sysfs.add_camera("4-1", "046d", "0825", nodes=2)  # C270, no serial
        cams = self._run(box_scanner)
        self.assertEqual(len(cams), 1)
        self.assertIsNone(cams[0]["serial"])
        self.assertEqual(cams[0]["address"], "usb-port:4-1")

    def test_missing_v4l_root(self):
        self.assertEqual(
            box_scanner._by_camera(v4l_root=self.sysfs.root / "nope"), [])

    def test_every_catalog_webcam_is_detectable(self):
        webcams = {
            name: meta for name, meta in box_scanner.SUPPORTED_USB.items()
            if "webcam" in meta["net_type"]
        }
        self.assertIn("Logitech_4K_Pro", webcams)
        self.assertEqual(
            {(m["vid"], m["pid"]) for m in webcams.values()},
            box_scanner._WEBCAM_VIDPIDS,
        )

    def test_global_shutter_camera_beside_a_logitech(self):
        # A laptop's built-in camera (not in the catalog, four nodes), a Logi
        # 4K Pro (four) and an ELP global-shutter camera (two: capture and
        # metadata), in the order a box enumerates them. The built-in one is
        # a UVC camera like the others.
        self.sysfs.add_camera("1-5", "0bda", "5520", serial="200901010001", nodes=4)
        self.sysfs.add_camera("1-1.2", "046d", "087f", serial="ABC123", nodes=4)
        first = self.sysfs.add_camera("1-1.4", "32e4", "0234", serial="01.00.00", nodes=2)
        self.assertEqual(first, 8)
        by_name = {c["name"]: c for c in self._run(box_scanner)}
        self.assertEqual(set(by_name), {box_scanner.GENERIC_WEBCAM_NAME,
                                        "Logitech_4K_Pro", "ELP_Global_Shutter_AR0234"})
        self.assertEqual(by_name[box_scanner.GENERIC_WEBCAM_NAME]["channels"],
                         {"webcam": ["/dev/video0"]})
        self.assertEqual(by_name["Logitech_4K_Pro"]["channels"], {"webcam": ["/dev/video4"]})
        elp = by_name["ELP_Global_Shutter_AR0234"]
        self.assertEqual(elp["channels"], {"webcam": ["/dev/video8"]})
        self.assertEqual((elp["vid"], elp["pid"], elp["serial"]), ("32e4", "0234", "01.00.00"))
        self.assertEqual(elp["address"], "USB0::0x32E4::0x0234::01.00.00::INSTR")

    def test_two_global_shutter_cameras_are_addressed_by_port(self):
        # The camera's serial is a fixed firmware string, so two of them can
        # only be told apart by where they are plugged in.
        self.sysfs.add_camera("1-1.3", "32e4", "0234", serial="01.00.00", nodes=2)
        self.sysfs.add_camera("1-1.4", "32e4", "0234", serial="01.00.00", nodes=2)
        cams = self._run(box_scanner)
        self.assertEqual([c["channels"]["webcam"] for c in cams],
                         [["/dev/video0"], ["/dev/video2"]])
        self.assertEqual([c["address"] for c in cams],
                         ["usb-port:1-1.3", "usb-port:1-1.4"])

    def test_one_global_shutter_camera_keeps_its_visa_address(self):
        self.sysfs.add_camera("1-1.3", "32e4", "0234", serial="01.00.00", nodes=2)
        self.assertEqual(self._run(box_scanner)[0]["address"],
                         "USB0::0x32E4::0x0234::01.00.00::INSTR")

    def test_two_cameras_with_no_serial_are_addressed_by_port(self):
        self.sysfs.add_camera("1-1.3", "dead", "beef", nodes=2)
        self.sysfs.add_camera("1-1.4", "dead", "beef", nodes=2)
        self.assertEqual([c["address"] for c in self._run(box_scanner)],
                         ["usb-port:1-1.3", "usb-port:1-1.4"])

    def test_document_camera_without_a_serial(self):
        # An IPEVO V4K PRO (two nodes, capture and metadata, and no serial)
        # beside a C920, as a bench with a document camera enumerates them.
        self.sysfs.add_camera("1-1", "046d", "082d", serial="C920SER", nodes=2)
        first = self.sysfs.add_camera("1-7", "1778", "d00e", nodes=2)
        self.assertEqual(first, 2)
        by_name = {c["name"]: c for c in self._run(box_scanner)}
        self.assertEqual(set(by_name), {"Logitech_C920", "IPEVO_V4K_PRO"})
        ipevo = by_name["IPEVO_V4K_PRO"]
        self.assertEqual(ipevo["channels"], {"webcam": ["/dev/video2"]})
        self.assertEqual(ipevo["net_type"], ["webcam"])
        self.assertIsNone(ipevo["serial"])
        self.assertEqual(ipevo["address"], "usb-port:1-7")


class ListInstrumentsTests(unittest.TestCase):
    """The whole scan, where a camera could also be picked up by ``scan_usb``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sysfs = FakeSysfs(Path(self._tmp.name))

    def _list(self):
        real_path, by_camera, by_card = (
            Path, box_scanner._by_camera, box_scanner._by_capture_card)
        v4l = self.sysfs.v4l
        sys_usb = self.sysfs.usb

        def fake_path(*args):
            return sys_usb if args == ("/sys/bus/usb/devices",) else real_path(*args)

        with patch.object(box_scanner, "Path", fake_path), \
                patch.object(box_scanner, "custom_instruments", return_value=[]), \
                patch.object(box_scanner, "_by_handshake", return_value=[]), \
                patch.object(box_scanner, "_saved_net_ttys", return_value=set()), \
                patch.object(box_scanner, "_saved_arm_serials", return_value=set()), \
                patch.object(box_scanner, "_by_camera", lambda: by_camera(v4l_root=v4l)), \
                patch.object(box_scanner, "_by_capture_card", lambda: by_card(v4l_root=v4l)):
            return box_scanner.list_instruments()

    def test_a_catalog_camera_is_listed_once_with_its_node(self):
        self.sysfs.add_camera("1-1", "046d", "082d", serial="S1", nodes=2)
        cams = self._list()
        self.assertEqual([c["name"] for c in cams], ["Logitech_C920"])
        self.assertEqual(cams[0]["channels"], {"webcam": ["/dev/video0"]})

    def test_identical_catalog_cameras_stay_two_addressable_entries(self):
        self.sysfs.add_camera("1-1.3", "32e4", "0234", serial="01.00.00", nodes=2)
        self.sysfs.add_camera("1-1.4", "32e4", "0234", serial="01.00.00", nodes=2)
        self.assertEqual([(c["address"], c["channels"]["webcam"]) for c in self._list()],
                         [("usb-port:1-1.3", ["/dev/video0"]),
                          ("usb-port:1-1.4", ["/dev/video2"])])

    def test_identical_unlisted_cameras_stay_two_entries(self):
        self.sysfs.add_camera("1-1.3", "dead", "beef", nodes=2)
        self.sysfs.add_camera("1-1.4", "dead", "beef", nodes=2)
        self.assertEqual(len(self._list()), 2)

    def test_a_catalog_camera_with_no_video_node_is_not_listed(self):
        self.sysfs.add_camera("1-1", "046d", "082d", serial="S1", nodes=0)
        self.assertEqual(self._list(), [])

    def test_camera_and_card_are_listed_apart(self):
        self.sysfs.add_camera("1-1", "dead", "beef", serial="S1", nodes=2)
        self.sysfs.add_camera("1-2", "534d", "2109", nodes=2)
        self.assertEqual(sorted((c["name"], c["net_type"][0]) for c in self._list()),
                         [("HDMI_Capture_MS2109", "capture"),
                          (box_scanner.GENERIC_WEBCAM_NAME, "webcam")])


class ResolveVideoDeviceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sysfs = FakeSysfs(Path(self._tmp.name))

    def _resolve(self, address, saved="/dev/video9"):
        return video_id.resolve_video_device(address, saved, v4l_root=self.sysfs.v4l)

    def test_a_port_address_finds_the_node_after_renumbering(self):
        self.sysfs.add_camera("1-1.3", "dead", "beef", nodes=2)
        self.sysfs.add_camera("1-1.4", "dead", "beef", nodes=2)
        self.assertEqual(self._resolve("usb-port:1-1.4"), "/dev/video2")

    def test_a_port_is_matched_whole_not_by_prefix(self):
        # 1-1 is the hub this camera sits behind, not the camera's port.
        self.sysfs.add_camera("1-1.4", "dead", "beef", nodes=2)
        with self.assertRaises(video_id.VideoDeviceNotFound) as caught:
            self._resolve("usb-port:1-1")
        self.assertIn("1-1.4", str(caught.exception))

    def test_an_empty_port_never_falls_back_to_the_saved_node(self):
        with self.assertRaises(video_id.VideoDeviceNotFound) as caught:
            self._resolve("usb-port:1-1.4")
        self.assertIn("no video device", str(caught.exception))

    def test_a_serial_finds_the_node_after_renumbering(self):
        self.sysfs.add_camera("1-1", "046d", "082d", serial="AAA", nodes=2)
        self.sysfs.add_camera("1-2", "046d", "082d", serial="BBB", nodes=2)
        self.assertEqual(self._resolve("USB0::0x046D::0x082D::BBB::INSTR"), "/dev/video2")

    def test_a_missing_serial_never_falls_back_to_the_saved_node(self):
        self.sysfs.add_camera("1-1", "046d", "082d", serial="AAA", nodes=2)
        with self.assertRaises(video_id.VideoDeviceNotFound) as caught:
            self._resolve("USB0::0x046D::0x082D::BBB::INSTR")
        self.assertIn("BBB", str(caught.exception))

    def test_a_shared_serial_uses_the_saved_node(self):
        self.sysfs.add_camera("1-1", "32e4", "0234", serial="01.00.00", nodes=2)
        self.sysfs.add_camera("1-2", "32e4", "0234", serial="01.00.00", nodes=2)
        self.assertEqual(self._resolve("USB0::0x32E4::0x0234::01.00.00::INSTR"),
                         "/dev/video9")

    def test_an_address_with_no_serial_uses_the_saved_node(self):
        self.assertEqual(self._resolve("USB0::0x046D::0x0825::::INSTR"), "/dev/video9")

    def test_no_address_uses_the_saved_node(self):
        self.assertEqual(self._resolve(None), "/dev/video9")

    def test_an_address_in_neither_scheme_uses_the_saved_node(self):
        self.assertEqual(self._resolve("TCPIP0::10.0.0.5::INSTR"), "/dev/video9")


if __name__ == "__main__":
    unittest.main()


class ByCaptureCardTests(unittest.TestCase):
    """HDMI capture cards, which a scope net is paired with for its live view."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sysfs = FakeSysfs(Path(self._tmp.name))

    def test_identical_cards_are_told_apart_by_their_port(self):
        # Two MS2109 cards, neither with a serial: the same in every way but
        # where they are plugged in.
        self.sysfs.add_camera("1-1.3", "534d", "2109", nodes=2)
        self.sysfs.add_camera("1-1.4", "534d", "2109", nodes=2)
        cards = box_scanner._by_capture_card(v4l_root=self.sysfs.v4l)
        self.assertEqual([c["port_path"] for c in cards], ["1-1.3", "1-1.4"])
        self.assertEqual([c["channels"] for c in cards],
                         [{"capture": ["/dev/video0"]}, {"capture": ["/dev/video2"]}])
        self.assertTrue(all(c["net_type"] == ["capture"] for c in cards))
        self.assertEqual(cards[0]["address"], "usb-port:1-1.3")

    def test_a_webcam_is_not_a_capture_card_nor_a_card_a_webcam(self):
        self.sysfs.add_camera("1-1", "046d", "082d", serial="C920SER", nodes=2)
        self.sysfs.add_camera("1-2", "534d", "2109", nodes=2)
        cards = box_scanner._by_capture_card(v4l_root=self.sysfs.v4l)
        cams = box_scanner._by_camera(v4l_root=self.sysfs.v4l)
        self.assertEqual([c["name"] for c in cards], ["HDMI_Capture_MS2109"])
        self.assertEqual([c["name"] for c in cams], ["Logitech_C920"])
