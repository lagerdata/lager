# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""``/dev/serial/by-id`` and ``by-path`` names on a macOS Box.

A net saved on a Linux Box may name its port by the link udev made for it.
Covers the agent working those names out from USB identity exactly as
udev 252 does (``60-serial.rules`` and the ``usb_id`` builtin), the Mac's own
by-path names, a by-path name from another machine being refused with a
reason, the agent's table answering links (open, exists, follow, glob, the
cable list, a USB identity), one session per port whichever name opens it,
the container sending links to the agent, and the Linux side of each new
``serial_id`` seam being the ``os.path`` call it replaced.

A pseudo-terminal stands in for the Mac device; no hardware or ``ioreg``.
"""
import errno
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import serial

from lager.devices import serial_id
from lager.host import darwin_scan, darwin_serial, darwin_udev, serial_rpc
from lager.hostlink import serial_proxy
from lager.protocols.uart import uart_bridge


class IdSerialTests(unittest.TestCase):
    """by-id names, against links udev makes for common USB serial chips."""

    def _name(self, vid, pid, manufacturer, product, serial_number, interface=0,
              adapter=True):
        return darwin_udev.by_id_name(vid, pid, manufacturer, product, serial_number,
                                      interface, adapter)

    def test_common_chips(self):
        cases = [
            (("0403", "6001", "FTDI", "FT232R USB UART", "A50285BI"),
             "usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0"),
            (("10c4", "ea60", "Silicon Labs", "CP2102 USB to UART Bridge Controller", "0001"),
             "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0"),
            (("067b", "2303", "Prolific Technology Inc.", "USB-Serial Controller", None),
             "usb-Prolific_Technology_Inc._USB-Serial_Controller-if00-port0"),
            # No manufacturer string: the vendor ID stands in for it.
            (("1a86", "7523", None, "USB2.0-Serial", None), "usb-1a86_USB2.0-Serial-if00-port0"),
        ]
        for args, expected in cases:
            self.assertEqual(self._name(*args), expected, args)

    def test_cdc_acm_device_has_no_port_suffix(self):
        # "Arduino (www.arduino.cc)": the parentheses become underscores;
        # no product string, so the product ID stands in.
        self.assertEqual(
            self._name("2341", "0043", "Arduino (www.arduino.cc)", None,
                       "95735353032351E0E0E1", adapter=False),
            "usb-Arduino__www.arduino.cc__0043_95735353032351E0E0E1-if00")

    def test_interface_number_is_two_hex_digits(self):
        self.assertEqual(self._name("0403", "6011", "FTDI", "Quad RS232-HS", None, 3),
                         "usb-FTDI_Quad_RS232-HS-if03-port0")
        self.assertTrue(self._name("0403", "6011", "FTDI", "X", None, 10).endswith("-if0a-port0"))

    def test_whitespace_is_trimmed_and_each_run_is_one_underscore(self):
        self.assertEqual(darwin_udev.id_serial("0001", "0002", "  Acme \t Labs ", " Box\n2 ", None),
                         "Acme_Labs_Box_2")

    def test_disallowed_characters_become_underscores_utf8_is_kept(self):
        self.assertEqual(darwin_udev.id_serial("0001", "0002", "Müller/Co", "a*b", None),
                         "Müller_Co_a_b")

    def test_serial_with_a_comma_or_non_ascii_byte_is_left_out(self):
        for bad in ("12,34", "12\x01", "Ünï"):
            self.assertEqual(darwin_udev.id_serial("0001", "0002", "V", "M", bad), "V_M", bad)
        self.assertEqual(darwin_udev.id_serial("0001", "0002", "V", "M", " A B "), "V_M_A_B")

    def test_vendor_and_model_are_cut_where_udev_cuts_them(self):
        self.assertEqual(darwin_udev.id_serial("0001", "0002", "x" * 80, "M", None),
                         "x" * 63 + "_M")

    def test_ids_are_lowercase_hex(self):
        self.assertEqual(darwin_udev.id_serial("0A5C", "21E8", None, None, None), "0a5c_21e8")


class ByPathTests(unittest.TestCase):
    def test_mac_names_follow_the_socket(self):
        self.assertEqual(darwin_udev.by_path_name("1-1.2", 0, True),
                         "platform-macos-usb1-usb-0:1.2:1.0-port0")
        self.assertEqual(darwin_udev.by_path_name("2-3", 2, False),
                         "platform-macos-usb2-usb-0:3:1.2")

    def test_a_linux_machines_by_path_is_refused_with_a_reason(self):
        message = darwin_udev.not_here_message(
            "/dev/serial/by-path/pci-0000:00:14.0-usb-0:1.2:1.0-port0")
        self.assertIn("do not carry over", message)
        self.assertIn("by-id", message)
        for path in ("/dev/serial/by-path/platform-macos-usb1-usb-0:1.2:1.0-port0",
                     "/dev/serial/by-id/usb-X-if00", "/dev/ttyUSB0"):
            self.assertIsNone(darwin_udev.not_here_message(path), path)


def _device(location, vid, pid, serial_number=None, product=None, manufacturer=None):
    return {"sysfs_name": f"{location:08x}", "vid": vid, "pid": pid,
            "serial": serial_number, "product": product, "manufacturer": manufacturer}


class LinksTableTests(unittest.TestCase):
    def test_every_port_gets_both_links(self):
        devices = [_device(0x01120000, "0403", "6010", "FT1", "Dual RS232-HS", "FTDI"),
                   _device(0x01130000, "1366", "1025", "000683935549", "J-Link", "SEGGER")]
        ports = [{"device": "/dev/cu.usbserial-FT1A", "location": 0x01120000, "interface": 0},
                 {"device": "/dev/cu.usbserial-FT1B", "location": 0x01120000, "interface": 1},
                 {"device": "/dev/cu.usbmodem0006839355491", "location": 0x01130000,
                  "interface": 0}]
        names = darwin_serial.Names().sync(p["device"] for p in ports)
        table = darwin_udev.links(devices, ports, names, darwin_scan.sysfs_name)
        self.assertEqual(table, {
            "/dev/serial/by-id/usb-FTDI_Dual_RS232-HS_FT1-if00-port0": "/dev/ttyUSB0",
            "/dev/serial/by-id/usb-FTDI_Dual_RS232-HS_FT1-if01-port0": "/dev/ttyUSB1",
            "/dev/serial/by-id/usb-SEGGER_J-Link_000683935549-if00": "/dev/ttyACM0",
            "/dev/serial/by-path/platform-macos-usb1-usb-0:1.2:1.0-port0": "/dev/ttyUSB0",
            "/dev/serial/by-path/platform-macos-usb1-usb-0:1.2:1.1-port0": "/dev/ttyUSB1",
            "/dev/serial/by-path/platform-macos-usb1-usb-0:1.3:1.0": "/dev/ttyACM0",
        })

    def test_identical_serial_less_adapters_share_one_by_id_link(self):
        devices = [_device(0x01120000, "1a86", "7523", None, "USB2.0-Serial"),
                   _device(0x01130000, "1a86", "7523", None, "USB2.0-Serial")]
        ports = [{"device": "/dev/cu.usbserial-110", "location": 0x01120000, "interface": 0},
                 {"device": "/dev/cu.usbserial-120", "location": 0x01130000, "interface": 0}]
        names = darwin_serial.Names().sync(p["device"] for p in ports)
        table = darwin_udev.links(devices, ports, names, darwin_scan.sysfs_name)
        self.assertEqual(table["/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00-port0"],
                         "/dev/ttyUSB0")
        # Each still has its own by-path link.
        self.assertEqual(sorted(v for k, v in table.items() if "by-path" in k),
                         ["/dev/ttyUSB0", "/dev/ttyUSB1"])

    def test_port_without_a_known_device_gets_no_links(self):
        ports = [{"device": "/dev/cu.usbserial-X", "location": 0x01120000, "interface": 0}]
        names = darwin_serial.Names().sync(p["device"] for p in ports)
        self.assertEqual(darwin_udev.links([], ports, names, darwin_scan.sysfs_name), {})


BY_ID = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0"
BY_PATH = "/dev/serial/by-path/platform-macos-usb1-usb-0:1.2:1.0-port0"
LINUX_BY_PATH = "/dev/serial/by-path/pci-0000:00:14.0-usb-0:1.2:1.0-port0"


class _OnePort(unittest.TestCase):
    """The agent as on a Mac, with one FT232R on a pseudo-terminal."""

    def setUp(self):
        for target, name in ([(serial, "Serial"), (darwin_serial, "NAMES"),
                              (darwin_scan, "_root"), (darwin_scan, "_refresh_locked"),
                              (serial_id, "_SYS_TTY"), (uart_bridge, "Path")]
                             + [(serial_id, n) for n in darwin_scan.REPLACED_IN_SERIAL_ID]):
            patcher = patch.object(target, name, getattr(target, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.master, slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, slave)
        self.device = os.ttyname(slave)
        self.attached = True
        darwin_serial.NAMES = darwin_serial.Names()
        darwin_scan._root = None
        darwin_scan._refresh_locked = self._scan
        darwin_scan.install_serial(os.path.join(self.tmp.name, "sysfs"))
        darwin_scan.install_fresh_lookups()
        darwin_serial.install_pyserial(refresh=darwin_scan.refresh)

    def _scan(self):
        devices, ports = [], []
        if self.attached:
            devices = [_device(0x01120000, "0403", "6001", "A50285BI", "FT232R USB UART", "FTDI")]
            ports = [{"device": self.device, "location": 0x01120000, "interface": 0}]
        names = darwin_serial.NAMES.sync(p["device"] for p in ports)
        darwin_scan.render(devices, darwin_scan._root, ports, names)
        darwin_serial.NAMES.set_links(
            darwin_udev.links(devices, ports, names, darwin_scan.sysfs_name))


class AgentLinkTests(_OnePort):
    def test_by_id_and_by_path_open_the_device(self):
        for link in (BY_ID, BY_PATH):
            port = serial.Serial(link, timeout=1)
            os.write(self.master, b"hi\n")
            self.assertEqual(port.readline(), b"hi\n", link)
            self.assertEqual(port.port, link)
            port.close()

    def test_linux_by_path_is_refused_with_the_reason(self):
        with self.assertRaises(serial.SerialException) as caught:
            serial.Serial(LINUX_BY_PATH)
        self.assertEqual(caught.exception.errno, 2)
        self.assertIn("do not carry over", str(caught.exception))

    def test_absent_by_id_fails_as_on_linux(self):
        self.attached = False
        with self.assertRaises(serial.SerialException) as caught:
            serial.Serial(BY_ID)
        self.assertEqual(caught.exception.errno, 2)
        self.assertIn("No such file or directory", str(caught.exception))

    def test_serial_id_answers_links(self):
        self.assertTrue(serial_id.port_exists(BY_ID))
        self.assertEqual(serial_id.resolve_link(BY_ID), "/dev/ttyUSB0")
        self.assertEqual(serial_id.resolve_link(BY_PATH), "/dev/ttyUSB0")
        self.assertEqual(serial_id.glob_ports("/dev/serial/by-id/*A50285BI*"), [BY_ID])
        self.attached = False
        self.assertFalse(serial_id.port_exists(BY_ID))
        self.assertEqual(serial_id.resolve_link(BY_ID), BY_ID)

    def test_cable_list_reports_the_links(self):
        cable, = serial_id.list_cables()
        self.assertEqual((cable["tty"], cable["by_id"], cable["by_path"]),
                         ("/dev/ttyUSB0", BY_ID, BY_PATH))

    def test_identity_of_a_link_is_the_identity_of_its_port(self):
        self.assertEqual(serial_id.identity_for_tty(BY_ID),
                         serial_id.identity_for_tty("/dev/ttyUSB0"))
        self.assertEqual(serial_id.identity_for_tty(BY_ID)["serial"], "A50285BI")
        self.assertIsNone(serial_id.identity_for_tty(LINUX_BY_PATH))

    def test_ea_finds_its_port_by_id(self):
        from lager.power.supply import ea

        self.assertEqual(ea._find_serial_device_path("A50285BI"), BY_ID)


class SessionTests(_OnePort):
    """The serial route on a Mac: one session per port, whatever its name."""

    def setUp(self):
        super().setUp()
        for target, name, value in ((sys, "platform", "darwin"),
                                    (darwin_scan, "refresh", self._scan)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(serial_rpc.close_all)

    def test_a_port_opened_by_link_is_busy_under_its_linux_name(self):
        serial_rpc.open_port(BY_ID)
        with self.assertRaises(OSError) as caught:
            serial_rpc.open_port("/dev/ttyUSB0")
        self.assertEqual(caught.exception.errno, 11)
        self.assertTrue(serial_rpc.close_port(BY_PATH))

    def test_a_linux_by_path_is_refused_with_the_reason(self):
        with self.assertRaises(FileNotFoundError) as caught:
            serial_rpc.open_port(LINUX_BY_PATH)
        self.assertEqual(caught.exception.errno, errno.ENOENT)
        self.assertIn("do not carry over", str(caught.exception))

    def test_lookup_route_allows_the_new_lookups(self):
        from flask import Flask

        client = serial_rpc.install(Flask(__name__)).test_client()
        for function, args, expected in (("port_exists", [BY_ID], True),
                                         ("resolve_link", [BY_ID], "/dev/ttyUSB0")):
            reply = client.post(serial_rpc.LOOKUP_PATH,
                                json={"function": function, "args": args})
            self.assertEqual((reply.status_code, reply.get_json()["result"]),
                             (200, expected), function)


class ContainerTests(unittest.TestCase):
    def test_new_lookups_have_safe_answers_when_the_host_cannot(self):
        namespace = {}
        with patch.object(serial_proxy, "lookup", lambda *a, **k: None):
            serial_proxy.install_lookups(namespace)
            self.assertIs(namespace["port_exists"]("/dev/ttyUSB0"), False)
            self.assertEqual(namespace["resolve_link"](BY_ID), BY_ID)
            self.assertEqual(namespace["list_cables"](), [])
            self.assertIsNone(namespace["resolve_tty"]("0403", "6001"))

    def test_new_lookups_ask_the_host(self):
        namespace = {}
        asked = []
        with patch.object(serial_proxy, "lookup",
                          lambda f, *a, **k: asked.append((f, a)) or "/dev/ttyUSB0"):
            serial_proxy.install_lookups(namespace)
            self.assertEqual(namespace["resolve_link"](BY_ID), "/dev/ttyUSB0")
        self.assertEqual(asked, [("resolve_link", (BY_ID,))])


class LinuxSeamTests(unittest.TestCase):
    """On a Linux Box ``serial_id.resolve_link`` is ``os.path.realpath``."""

    def test_same_answer_as_realpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "ttyUSB0")
            open(target, "w").close()
            link = os.path.join(tmp, "usb-X-if00-port0")
            os.symlink(target, link)
            for path in (link, target, os.path.join(tmp, "gone")):
                self.assertEqual(serial_id.resolve_link(path), os.path.realpath(path))


if __name__ == "__main__":
    unittest.main()
