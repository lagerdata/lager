# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""USB discovery for a Box container whose USB is on a macOS host.

Covers the agent's sysfs-shaped rendering of the Mac's USB bus, the Box
scanner run against it, the two routes the agent serves, the container's
stand-ins, and the two switches in Linux files (default off).

The device entries are constructed to match what one bench reported: an
Acroname USBHub3p with a J-Link behind it, a Keysight E36312A, a LabJack U3
and a PicoScope 2000.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from lager.host import darwin_scan, darwin_usb, usb_rpc
from lager.hostlink import usb_proxy
from lager.util import self_restart


def _device(location, vid, pid, serial=None, product=None):
    return {"sysfs_name": f"{location:08x}", "vid": f"{vid:04x}", "pid": f"{pid:04x}",
            "serial": serial, "product": product, "manufacturer": None,
            "busnum": None, "devnum": "1", "devpath": None,
            "device_class": None, "speed": None}


BENCH = [
    _device(0x01100000, 0x05E3, 0x0610, product="USB2.1 Hub"),
    _device(0x01110000, 0x24FF, 0x8013, "EC077668", "USBHub3p-2[A]"),
    _device(0x01112000, 0x24FF, 0x0013, "EC077668", "USBHub3p-Stem"),
    _device(0x01114000, 0x24FF, 0x8013, "EC077668", "USBHub3p-2[0-3]"),
    _device(0x01114400, 0x1366, 0x1025, "000683935549", "J-Link"),
    _device(0x01120000, 0x2A8D, 0x1102, "MY59001048"),
    _device(0x01130000, 0x0CD5, 0x0003, product="LabJack U3"),
    _device(0x02100000, 0x0CE9, 0x1016, product="PicoScope 2000"),
]


class SysfsNameTests(unittest.TestCase):
    def test_location_becomes_bus_and_port_path(self):
        self.assertEqual(darwin_scan.sysfs_name(0x01114400), "1-1.1.4.4")
        self.assertEqual(darwin_scan.sysfs_name(0x02100000), "2-1")
        self.assertEqual(darwin_scan.sysfs_name(0x14ABCDEF), "20-10.11.12.13.14.15")

    def test_root_hub_has_no_name(self):
        self.assertIsNone(darwin_scan.sysfs_name(0x01000000))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "sysfs")

    def _read(self, name, attribute):
        with open(os.path.join(self.root, "devices", name, attribute)) as fh:
            return fh.read().strip()

    def test_each_device_is_listed_with_its_attributes(self):
        darwin_scan.render(BENCH, self.root)
        self.assertEqual(self._read("1-1.1.4.4", "idVendor"), "1366")
        self.assertEqual(self._read("1-1.1.4.4", "idProduct"), "1025")
        self.assertEqual(self._read("1-1.1.4.4", "serial"), "000683935549")
        self.assertEqual(self._read("2-1", "product"), "PicoScope 2000")

    def test_missing_attribute_is_a_missing_file(self):
        darwin_scan.render(BENCH, self.root)
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "devices", "1-1.3", "serial")))

    def test_device_directory_sits_inside_its_parent_hub(self):
        darwin_scan.render(BENCH, self.root)
        listing = os.path.join(self.root, "devices")
        jlink = os.path.realpath(os.path.join(listing, "1-1.1.4.4"))
        hub = os.path.realpath(os.path.join(listing, "1-1.1.4"))
        self.assertEqual(os.path.dirname(jlink), hub)

    def test_each_bus_has_a_root_hub_under_its_own_controller(self):
        darwin_scan.render(BENCH, self.root)
        listing = os.path.join(self.root, "devices")
        one = os.path.dirname(os.path.realpath(os.path.join(listing, "usb1")))
        two = os.path.dirname(os.path.realpath(os.path.join(listing, "usb2")))
        self.assertNotEqual(one, two)

    def test_rendering_again_drops_a_device_that_left(self):
        darwin_scan.render(BENCH, self.root)
        darwin_scan.render([d for d in BENCH if d["vid"] != "1366"], self.root)
        self.assertFalse(os.path.lexists(os.path.join(self.root, "devices", "1-1.1.4.4")))
        self.assertTrue(os.path.lexists(os.path.join(self.root, "devices", "1-1.2")))

    def test_entry_without_a_usable_location_is_skipped(self):
        broken = dict(BENCH[0], sysfs_name="not-hex")
        darwin_scan.render([broken, _device(0x01000000, 1, 2)], self.root)
        self.assertEqual(os.listdir(os.path.join(self.root, "devices")), [])

    def test_serial_port_is_a_tty_entry_inside_its_usb_interface(self):
        adapter = _device(0x01114300, 0x10C4, 0xEA60, "0001", "CP2102")
        ports = [{"device": "/dev/cu.usbserial-0001", "location": 0x01114300, "interface": None},
                 {"device": "/dev/cu.usbserial-B", "location": 0x01114300, "interface": 1},
                 {"device": "/dev/cu.unnamed", "location": 0x01114300, "interface": 0},
                 {"device": "/dev/cu.orphan", "location": 0x01990000, "interface": 0}]
        names = {"/dev/cu.usbserial-0001": "ttyUSB0", "/dev/cu.usbserial-B": "ttyUSB1",
                 "/dev/cu.orphan": "ttyUSB2"}
        darwin_scan.render(BENCH + [adapter], self.root, ports, names)
        ttys = os.path.join(self.root, "class", "tty")
        # No Linux name, or no USB device at that location: not listed.
        self.assertEqual(sorted(os.listdir(ttys)), ["ttyUSB0", "ttyUSB1"])
        device = os.path.realpath(os.path.join(ttys, "ttyUSB1", "device"))
        self.assertEqual(os.path.basename(os.path.dirname(device)), "1-1.1.4.3:1.1")
        usb_dir = os.path.dirname(os.path.dirname(device))
        self.assertEqual(usb_dir, os.path.realpath(
            os.path.join(self.root, "devices", "1-1.1.4.3")))
        first = os.path.realpath(os.path.join(ttys, "ttyUSB0", "device"))
        self.assertEqual(os.path.basename(os.path.dirname(first)), "1-1.1.4.3:1.0")


class ScanTests(unittest.TestCase):
    """The Box's own scanner, reading the rendered tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "sysfs")
        scanner = darwin_scan._load_scanner(self.root)
        # Keep the scan to the rendered tree: no scope daemon, no serial
        # handshake, no cameras of whatever machine runs this test.
        scanner._pico_daemon_capabilities = lambda: None
        scanner._by_handshake = lambda **kwargs: []
        scanner._by_camera = lambda: []
        scanner._by_capture_card = lambda: []
        scanner.custom_instruments = lambda: []
        scanner._saved_net_ttys = lambda: set()
        scanner._saved_arm_serials = lambda: set()
        for name, value in (("_scanner", scanner), ("_root", self.root)):
            patcher = patch.object(darwin_scan, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.devices = list(BENCH)
        patcher = patch.object(darwin_usb, "enumerate_usb_devices", lambda: self.devices)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _scan(self):
        return {d["name"]: d for d in darwin_scan.list_instruments()}

    def test_first_scan_loads_the_scanner_and_picks_the_root(self):
        with patch.object(darwin_scan, "_scanner", None), \
                patch.object(darwin_scan, "_root", None), \
                patch.object(darwin_scan, "_load_scanner",
                             lambda root: darwin_scan._scanner_for_test) as _:
            darwin_scan._scanner_for_test = type(
                "S", (), {"list_instruments": staticmethod(lambda: ["seen"])})
            self.addCleanup(delattr, darwin_scan, "_scanner_for_test")
            self.assertEqual(darwin_scan.list_instruments(self.root), ["seen"])
            self.assertEqual(darwin_scan._root, self.root)

    def test_bench_instruments_are_found_with_linux_addresses(self):
        found = self._scan()
        self.assertEqual(found["KEYSIGHT_E36312A"]["address"],
                         "USB0::0x2A8D::0x1102::MY59001048::INSTR")
        self.assertEqual(found["J-Link"]["address"],
                         "USB0::0x1366::0x1025::000683935549::INSTR")
        self.assertEqual(found["Acroname_8Port"]["address"],
                         "USB0::0x24FF::0x0013::EC077668::INSTR")
        self.assertIn("Picoscope", found)

    def test_device_without_a_serial_is_addressed_by_its_port(self):
        self.assertEqual(self._scan()["LabJack_U3"]["address"],
                         "USB0::0x0CD5::0x0003::port-1-1.3::INSTR")

    def test_unsupported_devices_are_not_listed(self):
        self.assertNotIn("05e3", {d["vid"] for d in self._scan().values()})

    def test_each_scan_reads_the_bus_again(self):
        self.assertIn("J-Link", self._scan())
        self.devices = [d for d in BENCH if d["vid"] != "1366"]
        self.assertNotIn("J-Link", self._scan())

    def test_scanner_is_a_private_copy_not_the_box_module(self):
        self._scan()
        from lager.http_handlers import usb_scanner
        self.assertIsNot(darwin_scan._scanner, usb_scanner)
        # The Box's module still reads the real sysfs.
        self.assertEqual(str(usb_scanner._SYS_USB_DEVICES), "/sys/bus/usb/devices")
        self.assertEqual(str(usb_scanner.Path("/sys/bus/usb/devices")),
                         "/sys/bus/usb/devices")

    def test_only_usb_sysfs_paths_are_redirected(self):
        path = darwin_scan._redirected_path("/tmp/x")
        self.assertEqual(str(path("/sys/bus/usb/devices")), "/tmp/x/devices")
        self.assertEqual(str(path("/sys/bus/usb/devices/1-1")), "/tmp/x/devices/1-1")
        self.assertEqual(str(path("/sys/bus/usb/devicesX")), "/sys/bus/usb/devicesX")
        self.assertEqual(str(path("/sys/class/tty")), "/tmp/x/class/tty")
        self.assertEqual(str(path("/sys/class/tty/ttyUSB0/device")),
                         "/tmp/x/class/tty/ttyUSB0/device")
        self.assertEqual(str(path("/sys/class/video4linux")), "/sys/class/video4linux")
        self.assertEqual(str(path("/etc", "lager")), "/etc/lager")


class DiscoveryRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = usb_rpc.install(Flask(__name__)).test_client()

    def test_ids_are_served_as_a_list(self):
        ids = {(0x2A8D, 0x1102): ["MY59001048"], (0x0CD5, 3): [None]}
        with patch.object(self_restart, "enumerated_usb_ids", lambda: ids):
            body = self.client.get(usb_rpc.IDS_PATH).get_json()
        self.assertEqual(body, {"devices": [[0x0CD5, 3, [None]],
                                            [0x2A8D, 0x1102, ["MY59001048"]]]})

    def test_unknown_bus_is_null_and_empty_bus_is_empty(self):
        with patch.object(self_restart, "enumerated_usb_ids", lambda: None):
            self.assertEqual(self.client.get(usb_rpc.IDS_PATH).get_json(), {"devices": None})
        with patch.object(self_restart, "enumerated_usb_ids", lambda: {}):
            self.assertEqual(self.client.get(usb_rpc.IDS_PATH).get_json(), {"devices": []})

    def test_instruments_are_served(self):
        with patch.object(usb_rpc, "_list_instruments", lambda: [{"name": "J-Link"}]):
            resp = self.client.get(usb_rpc.INSTRUMENTS_PATH)
        self.assertEqual(resp.get_json(), {"instruments": [{"name": "J-Link"}]})

    def test_failed_scan_is_a_500_with_the_reason(self):
        def fail():
            raise OSError("ioreg went away")
        with patch.object(usb_rpc, "_list_instruments", fail):
            resp = self.client.get(usb_rpc.INSTRUMENTS_PATH)
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"error": "ioreg went away", "type": "OSError"})


class _Response:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text
        self.ok = status < 400

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class DiscoveryProxyTests(unittest.TestCase):
    def _get(self, reply):
        self.gets = []

        def get(url, timeout=None):
            self.gets.append((url, timeout))
            if isinstance(reply, Exception):
                raise reply
            return reply

        return patch.object(usb_proxy.requests, "get", get)

    def test_ids_come_back_in_the_sysfs_readers_shape(self):
        reply = _Response(200, {"devices": [[0x2A8D, 0x1102, ["MY59001048"]], [0x0CD5, 3, [None]]]})
        with self._get(reply):
            ids = usb_proxy.enumerated_usb_ids()
        self.assertEqual(ids, {(0x2A8D, 0x1102): ["MY59001048"], (0x0CD5, 3): [None]})
        self.assertEqual(self.gets[0][0], "http://localhost:8080/v1/usb/ids")
        self.assertTrue(self_restart.usb_address_enumerated(
            "USB0::0x2A8D::0x1102::MY59001048::INSTR", ids))

    def test_anything_but_a_device_list_is_unknown(self):
        for reply in (_Response(200, {"devices": None}), _Response(503, {"error": "x"}),
                      _Response(200, None, "<html>"), _Response(200, {"devices": [[1]]}),
                      ConnectionError("refused")):
            with self._get(reply):
                self.assertIsNone(usb_proxy.enumerated_usb_ids(), reply)

    def test_instrument_list_is_returned(self):
        with self._get(_Response(200, {"instruments": [{"name": "J-Link"}]})):
            self.assertEqual(usb_proxy.list_instruments(), [{"name": "J-Link"}])
        self.assertEqual(self.gets[0][0], "http://localhost:8080/v1/instruments/list")

    def test_failed_scan_raises_with_the_agents_reason(self):
        with self._get(_Response(500, {"error": "ioreg went away", "type": "OSError"})):
            with self.assertRaisesRegex(RuntimeError, "ioreg went away"):
                usb_proxy.list_instruments()
        with self._get(_Response(502, None, "bad gateway")):
            with self.assertRaisesRegex(RuntimeError, "bad gateway"):
                usb_proxy.list_instruments()
        with self._get(_Response(200, {"instruments": None})):
            with self.assertRaisesRegex(RuntimeError, "no instrument list"):
                usb_proxy.list_instruments()


class DiscoverySwitchTests(unittest.TestCase):
    """The two Linux files discovery touches: default off."""

    def _env(self, value):
        env = {k: v for k, v in os.environ.items() if k != "LAGER_HOSTLINK"}
        if value is not None:
            env["LAGER_HOSTLINK"] = value
        return patch.dict(os.environ, env, clear=True)

    def _refuse(self, *args, **kwargs):
        raise AssertionError("the host agent must not be asked")

    def test_usb_ids_come_from_sysfs_unless_switched(self):
        for value in (None, "0", "", "true"):
            with self._env(value), patch.object(usb_proxy, "enumerated_usb_ids", self._refuse), \
                    patch.object(self_restart.glob, "glob", lambda pattern: []) as _:
                self.assertEqual(self_restart.enumerated_usb_ids(), {}, value)

    def test_usb_ids_come_from_the_host_when_switched(self):
        ids = {(1, 2): ["s"]}
        with self._env("1"), patch.object(usb_proxy, "enumerated_usb_ids", lambda: ids), \
                patch.object(self_restart.glob, "glob", self._refuse):
            self.assertEqual(self_restart.enumerated_usb_ids(), ids)

    def test_instruments_are_scanned_here_unless_switched(self):
        from lager.http_handlers import usb_scanner
        for value in (None, "0", "", "true"):
            with self._env(value), patch.object(usb_proxy, "list_instruments", self._refuse), \
                    patch.object(usb_scanner, "scan_usb", lambda: []), \
                    patch.object(usb_scanner, "custom_instruments", lambda: []), \
                    patch.object(usb_scanner, "_by_handshake", lambda **kw: []), \
                    patch.object(usb_scanner, "_by_camera", lambda: []), \
                    patch.object(usb_scanner, "_by_capture_card", lambda: []), \
                    patch.object(usb_scanner, "_saved_net_ttys", lambda: set()), \
                    patch.object(usb_scanner, "_saved_arm_serials", lambda: set()):
                self.assertEqual(usb_scanner.list_instruments(), [], value)

    def test_instruments_come_from_the_host_when_switched(self):
        from lager.http_handlers import usb_scanner
        with self._env("1"), patch.object(usb_proxy, "list_instruments",
                                          lambda: [{"name": "J-Link"}]), \
                patch.object(usb_scanner, "scan_usb", self._refuse):
            self.assertEqual(usb_scanner.list_instruments(), [{"name": "J-Link"}])


if __name__ == "__main__":
    unittest.main()
