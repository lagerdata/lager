# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Serial ports for a Box container whose USB is on a macOS host.

Covers the Linux names the agent gives the Mac's ports, the Box's own
lookups run against the agent's sysfs-shaped tree, the agent's routes, a
real RFC 2217 round trip between the container's stand-in and the agent's
session over loopback sockets (with pySerial's ``loop://`` in place of a
device), and the switches in Linux files (default off).

Registry data is constructed to match one bench: a CP2102 adapter and a
J-Link's CDC port behind an Acroname hub. No hardware or ``ioreg`` is used.
"""
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import serial
from flask import Flask

from lager.devices import serial_id
from lager.host import darwin_scan, darwin_serial, darwin_usb, serial_rpc
from lager.hostlink import serial_proxy
from lager.protocols.uart import uart_bridge

BOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "box")


def _usb(name, location, vid, pid, *children, **extra):
    node = {"IORegistryEntryName": name, "locationID": location,
            "idVendor": vid, "idProduct": pid}
    node.update(extra)
    if children:
        node["IORegistryEntryChildren"] = list(children)
    return node


def _interface(number, *children):
    return {"bInterfaceNumber": number, "IORegistryEntryChildren": list(children)}


def _tty(device):
    return {"IORegistryEntryChildren": [{"IOCalloutDevice": device,
                                         "IODialinDevice": device.replace("cu.", "tty.")}]}


CP2102 = _usb("CP2102", 0x01114300, 0x10C4, 0xEA60, _interface(0, _tty("/dev/cu.usbserial-0001")))
JLINK = _usb("J-Link", 0x01114400, 0x1366, 0x1025,
             _interface(0, _tty("/dev/cu.usbmodem0006839355491")), _interface(2))
FT2232 = _usb("FT2232H", 0x01120000, 0x0403, 0x6010,
              _interface(0, _tty("/dev/cu.usbserial-FT1A")),
              _interface(1, _tty("/dev/cu.usbserial-FT1B")))


class ParsePortsTests(unittest.TestCase):
    def _parse(self, *roots):
        return darwin_serial.parse_ports(plistlib.dumps(list(roots)))

    def test_port_carries_its_usb_device_and_interface(self):
        self.assertEqual(self._parse(CP2102), [
            {"device": "/dev/cu.usbserial-0001", "location": 0x01114300, "interface": 0}])

    def test_each_interface_of_a_two_port_chip_is_a_port(self):
        self.assertEqual([(p["device"], p["interface"]) for p in self._parse(FT2232)],
                         [("/dev/cu.usbserial-FT1A", 0), ("/dev/cu.usbserial-FT1B", 1)])

    def test_device_listed_under_its_hub_and_again_alone_is_one_port(self):
        hub = _usb("Hub", 0x01110000, 0x24FF, 0x8013, CP2102, JLINK)
        ports = self._parse(hub, CP2102, JLINK)
        self.assertEqual([p["device"] for p in ports],
                         ["/dev/cu.usbmodem0006839355491", "/dev/cu.usbserial-0001"])
        self.assertEqual(ports[1]["location"], 0x01114300)

    def test_interface_does_not_leak_from_a_hub_into_the_device_below(self):
        bare = _usb("Odd", 0x01114300, 1, 2, _tty("/dev/cu.usbserial-X"))
        hub = _usb("Hub", 0x01110000, 3, 4, _interface(5, bare))
        self.assertEqual(self._parse(hub), [
            {"device": "/dev/cu.usbserial-X", "location": 0x01114300, "interface": None}])

    def test_port_outside_any_usb_device_and_empty_output_give_nothing(self):
        self.assertEqual(self._parse(_tty("/dev/cu.Bluetooth-Incoming-Port")), [])
        self.assertEqual(darwin_serial.parse_ports(b""), [])

    def test_unreadable_registry_is_no_ports(self):
        with patch.object(darwin_serial.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("ioreg", 5)):
            self.assertEqual(darwin_serial.enumerate_ports(), [])


class NamesTests(unittest.TestCase):
    def test_adapters_are_ttyusb_and_cdc_devices_are_ttyacm(self):
        names = darwin_serial.Names()
        self.assertEqual(names.sync(["/dev/cu.usbserial-0001", "/dev/cu.usbmodem1", "/dev/cu.wchusbserial9"]),
                         {"/dev/cu.usbserial-0001": "ttyUSB0", "/dev/cu.usbmodem1": "ttyACM0",
                          "/dev/cu.wchusbserial9": "ttyUSB1"})

    def test_a_port_keeps_its_name_while_others_come_and_go(self):
        names = darwin_serial.Names()
        names.sync(["/dev/cu.usbserial-A", "/dev/cu.usbserial-B"])
        self.assertEqual(names.sync(["/dev/cu.usbserial-B"]), {"/dev/cu.usbserial-B": "ttyUSB1"})
        # The freed number is the next one handed out, as on Linux.
        self.assertEqual(names.sync(["/dev/cu.usbserial-B", "/dev/cu.usbserial-C"]),
                         {"/dev/cu.usbserial-B": "ttyUSB1", "/dev/cu.usbserial-C": "ttyUSB0"})

    def test_linux_name_maps_back_to_the_mac_device(self):
        names = darwin_serial.Names()
        names.sync(["/dev/cu.usbserial-0001"])
        self.assertEqual(names.host_device("/dev/ttyUSB0"), "/dev/cu.usbserial-0001")
        self.assertEqual(names.host_device("ttyUSB0"), "/dev/cu.usbserial-0001")
        self.assertIsNone(names.host_device("/dev/ttyUSB1"))
        names.sync([])
        self.assertIsNone(names.host_device("/dev/ttyUSB0"))


def _entry(location, vid, pid, serial_number=None, product=None):
    return {"sysfs_name": f"{location:08x}", "vid": f"{vid:04x}", "pid": f"{pid:04x}",
            "serial": serial_number, "product": product, "manufacturer": None}


class BoxLookupsOnTheAgentTests(unittest.TestCase):
    """The Box's unedited lookups, reading the tree the agent writes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "sysfs")
        for target, name in ((serial_id, "_SYS_TTY"), (uart_bridge, "Path"),
                             (darwin_scan, "_root")):
            patcher = patch.object(target, name, getattr(target, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        darwin_scan._root = None
        darwin_scan.install_serial(root)
        devices = [_entry(0x01114300, 0x10C4, 0xEA60, "0001", "CP2102"),
                   _entry(0x01120000, 0x0403, 0x6010, "FT1", "FT2232H")]
        ports = [{"device": "/dev/cu.usbserial-0001", "location": 0x01114300, "interface": 0},
                 {"device": "/dev/cu.usbserial-FT1A", "location": 0x01120000, "interface": 0},
                 {"device": "/dev/cu.usbserial-FT1B", "location": 0x01120000, "interface": 1}]
        names = darwin_serial.Names().sync(p["device"] for p in ports)
        darwin_scan.render(devices, root, ports, names)

    def test_cables_are_listed_as_on_linux(self):
        cables = {c["tty"]: c for c in serial_id.list_cables()}
        self.assertEqual(sorted(cables), ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"])
        cp = cables["/dev/ttyUSB0"]
        self.assertEqual((cp["vid"], cp["pid"], cp["serial"], cp["port_path"], cp["interface"]),
                         ("10c4", "ea60", "0001", "1-1.1.4.3", 0))
        self.assertEqual(cables["/dev/ttyUSB2"]["interface"], 1)

    def test_identity_round_trips_to_the_right_channel(self):
        ident = serial_id.identity_for_tty("/dev/ttyUSB2")
        self.assertEqual(ident, {"vid": "0403", "pid": "6010", "serial": "FT1",
                                 "port_path": "1-1.2", "interface": 1})
        self.assertEqual(serial_id.resolve_identity(ident), "/dev/ttyUSB2")

    def test_bridge_finds_a_port_by_usb_serial(self):
        find = uart_bridge.UARTBridge._find_device_by_serial
        self.assertEqual(find(None, "0001"), "/dev/ttyUSB0")
        self.assertIsNone(find(None, "nope"))


class LookupRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = serial_rpc.install(Flask(__name__)).test_client()
        self.calls = []

        def lookup(function, args, kwargs):
            self.calls.append((function, args, kwargs))
            return [{"tty": "/dev/ttyUSB0"}]

        for name, value in (("_refresh", lambda: self.calls.append("refresh")),
                            ("_lookup", lookup)):
            patcher = patch.object(serial_rpc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_lookup_runs_after_the_bus_is_read_again(self):
        resp = self.client.post(serial_rpc.LOOKUP_PATH, json={
            "function": "resolve_tty", "args": ["10c4", "ea60"], "kwargs": {"serial": "0001"}})
        self.assertEqual(resp.get_json(), {"result": [{"tty": "/dev/ttyUSB0"}]})
        self.assertEqual(self.calls, ["refresh", ("resolve_tty", ["10c4", "ea60"], {"serial": "0001"})])

    def test_only_listed_lookups_are_run(self):
        for body in ({"function": "_read_sysfs_text"}, {"function": "parse_address"}, {}, None):
            self.assertEqual(self.client.post(serial_rpc.LOOKUP_PATH, json=body).status_code, 400)
        self.assertEqual(self.calls, [])

    def test_every_lookup_the_container_sends_is_served(self):
        for function in serial_proxy._LOOKUPS + ("find_device_by_serial",):
            self.assertIn(function, serial_rpc._LOOKUPS)

    def test_failed_lookup_is_a_500_with_the_reason(self):
        with patch.object(serial_rpc, "_lookup", side_effect=ValueError("bad identity")):
            resp = self.client.post(serial_rpc.LOOKUP_PATH, json={"function": "list_cables"})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"error": "bad identity", "type": "ValueError"})

    def test_open_and_close_need_a_port_name(self):
        for path in (serial_rpc.OPEN_PATH, serial_rpc.CLOSE_PATH):
            for body in ({}, {"port": ""}, {"port": 7}, None):
                self.assertEqual(self.client.post(path, json=body).status_code, 400)


class _HostLink:
    """Stands in for the relay: the stand-in's posts reach the routes directly."""

    def __init__(self, testcase):
        self.client = serial_rpc.install(Flask(__name__)).test_client()
        self.devices = []
        self.present = {"/dev/ttyUSB0": "/dev/cu.usbserial-0001"}

        def post(url, body):
            resp = self.client.post(url[len(serial_proxy.RELAY):], json=body)
            return resp.status_code < 400, resp.get_json()

        def open_device(path):
            device = serial.serial_for_url("loop://", timeout=0.05)
            device.opened_path = path
            self.devices.append(device)
            return device

        for target, name, value in (
                (serial_proxy, "_post", post),
                (serial_proxy, "_connect_host", lambda: "127.0.0.1"),
                (serial_proxy.os.path, "exists", lambda path: False),
                (serial_rpc, "_refresh", lambda: None),
                (serial_rpc, "_host_device", lambda tty: self.present.get(tty)),
                (serial_rpc, "_open_device", open_device),
                (serial_rpc, "BUSY_GRACE_S", 0.1)):
            patcher = patch.object(target, name, value)
            patcher.start()
            testcase.addCleanup(patcher.stop)
        testcase.addCleanup(serial_rpc.close_all)


class RoundTripTests(unittest.TestCase):
    """The container's port and the agent's session, over real sockets."""

    def setUp(self):
        self.link = _HostLink(self)

    def _open(self, **kwargs):
        port = serial_proxy.Serial("/dev/ttyUSB0", 115200, timeout=2, **kwargs)
        self.addCleanup(port.close)
        return port

    def test_host_port_opens_as_the_stand_in_under_its_linux_name(self):
        port = self._open(exclusive=True)
        self.assertIsInstance(port, serial_proxy.HostSerial)
        self.assertEqual(port.port, "/dev/ttyUSB0")
        self.assertEqual(self.link.devices[0].opened_path, "/dev/cu.usbserial-0001")

    def test_bytes_cross_both_ways_unchanged(self):
        port = self._open()
        payload = bytes(range(256)) * 4      # includes 0xFF, which the protocol escapes
        port.write(payload)
        self.assertEqual(port.read(len(payload)), payload)

    def test_line_settings_reach_the_device(self):
        port = self._open()
        port.baudrate = 9600
        port.parity = serial.PARITY_EVEN
        device = self.link.devices[0]
        self.assertEqual((device.baudrate, device.parity), (9600, serial.PARITY_EVEN))

    def test_second_open_fails_as_a_locked_port_does_on_linux(self):
        self._open()
        with self.assertRaises(serial.SerialException) as caught:
            serial_proxy.Serial("/dev/ttyUSB0")
        text = str(caught.exception)
        self.assertIn("[Errno 11]", text)
        self.assertIn("Could not exclusively lock port /dev/ttyUSB0", text)
        self.assertFalse(uart_bridge.UARTBridge.is_device_gone(caught.exception))

    def test_closing_releases_the_device_before_returning(self):
        for _ in range(3):
            port = serial_proxy.Serial("/dev/ttyUSB0", timeout=1)
            port.write(b"again")
            self.assertEqual(port.read(5), b"again")
            port.close()
            self.assertFalse(self.link.devices[-1].is_open)
        self.assertEqual(len(self.link.devices), 3)

    def test_absent_port_fails_as_a_missing_device_does(self):
        with self.assertRaises(serial.SerialException) as caught:
            serial_proxy.Serial("/dev/ttyUSB5")
        self.assertIn("No such file or directory: '/dev/ttyUSB5'", str(caught.exception))
        self.assertEqual(self.link.devices, [])

    def test_device_leaving_reads_as_device_gone(self):
        port = self._open()
        self.link.devices[0].close()         # what an unplugged adapter does to the agent
        with self.assertRaises(serial.SerialException) as caught:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                port.read(1)
        self.assertTrue(uart_bridge.UARTBridge.is_device_gone(caught.exception))

    def test_port_that_is_never_connected_to_is_released(self):
        with patch.object(serial_rpc, "ACCEPT_WAIT_S", 0.1):
            serial_rpc.open_port("/dev/ttyUSB0")
            deadline = time.monotonic() + 5
            while self.link.devices[0].is_open and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertFalse(self.link.devices[0].is_open)
        self._open()                         # and can be opened again

    def test_device_that_cannot_be_opened_names_the_linux_port(self):
        def refuse(path):
            raise serial.SerialException(f"could not open port {path}: denied")
        with patch.object(serial_rpc, "_open_device", refuse):
            with self.assertRaises(serial.SerialException) as caught:
                serial_proxy.Serial("/dev/ttyUSB0")
        self.assertIn("could not open port /dev/ttyUSB0: denied", str(caught.exception))
        self.assertNotIn("cu.usbserial", str(caught.exception))


class StandInSelectionTests(unittest.TestCase):
    def test_only_absent_linux_usb_serial_names_go_to_the_host(self):
        with patch.object(serial_proxy, "_device_exists", lambda path: False):
            for port in ("/dev/ttyUSB0", "/dev/ttyACM12"):
                self.assertTrue(serial_proxy.is_host_port(port), port)
            for port in ("/dev/ttyS0", "/dev/ttyUSB", "/dev/ttyUSB0x", "COM3", "loop://",
                         "rfc2217://h:1", "/dev/serial/by-id/x", None, 3):
                self.assertFalse(serial_proxy.is_host_port(port), port)

    def test_port_that_exists_in_the_container_is_opened_natively(self):
        with patch.object(serial_proxy, "_device_exists", lambda path: True):
            self.assertFalse(serial_proxy.is_host_port("/dev/ttyUSB0"))

    def test_other_ports_and_subclasses_get_the_native_class(self):
        port = serial_proxy.Serial()         # no port: nothing is opened
        self.assertIsInstance(port, serial_proxy._native_serial)

        class Mine(serial_proxy.Serial):
            pass

        with patch.object(serial_proxy, "_device_exists", lambda path: False):
            self.assertIsInstance(Mine(), Mine)

    def test_install_replaces_serial_once(self):
        with patch.object(serial, "Serial", serial_proxy._native_serial):
            serial_proxy.install_pyserial()
            self.assertIs(serial.Serial, serial_proxy.Serial)
            marker = serial.Serial
            serial_proxy.install_pyserial()
            self.assertIs(serial.Serial, marker)

    def test_lookups_fall_back_to_nothing_found(self):
        namespace = {}
        serial_proxy.install_lookups(namespace)
        with patch.object(serial_proxy, "_post", side_effect=ConnectionError("no relay")):
            self.assertEqual(namespace["list_cables"](), [])
            self.assertIsNone(namespace["resolve_identity"]({"vid": "10c4"}))
            self.assertIsNone(serial_proxy.find_device_by_serial("0001"))
        with patch.object(serial_proxy, "_post", lambda url, body: (False, {"error": "x"})):
            self.assertIsNone(namespace["identity_for_tty"]("/dev/ttyUSB0"))

    def test_lookup_sends_the_function_and_returns_its_result(self):
        sent = []

        def post(url, body):
            sent.append((url, body))
            return True, {"result": "/dev/ttyUSB0"}

        namespace = {}
        serial_proxy.install_lookups(namespace)
        with patch.object(serial_proxy, "_post", post):
            self.assertEqual(namespace["resolve_tty"]("10c4", "ea60", serial="0001"), "/dev/ttyUSB0")
        self.assertEqual(sent, [(serial_proxy.LOOKUP_URL, {
            "function": "resolve_tty", "args": ["10c4", "ea60"], "kwargs": {"serial": "0001"}})])


class SwitchTests(unittest.TestCase):
    """The Linux files the serial hand-off touches: default off."""

    PROBE = (
        "import serial, sys\n"
        "native = serial.Serial\n"
        "from lager.devices import serial_id\n"
        "print(serial_id.list_cables.__module__, serial.Serial is native)\n")

    def _probe(self, value, code=None, extra_path=()):
        env = {k: v for k, v in os.environ.items() if k != "LAGER_HOSTLINK"}
        if value is not None:
            env["LAGER_HOSTLINK"] = value
        env["PYTHONPATH"] = os.pathsep.join(list(extra_path) + [BOX])
        result = subprocess.run([sys.executable, "-c", code or self.PROBE], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_serial_id_is_untouched_unless_switched(self):
        for value in (None, "0", "", "true"):
            self.assertEqual(self._probe(value), "lager.devices.serial_id True", value)

    def test_serial_id_asks_the_host_when_switched(self):
        self.assertEqual(self._probe("1"), "lager.hostlink.serial_proxy False")

    def test_bridge_walks_sysfs_unless_switched(self):
        refuse = patch.object(serial_proxy, "find_device_by_serial",
                              side_effect=AssertionError("the host must not be asked"))
        for value in (None, "0", "", "true"):
            env = {k: v for k, v in os.environ.items() if k != "LAGER_HOSTLINK"}
            if value is not None:
                env["LAGER_HOSTLINK"] = value
            with patch.dict(os.environ, env, clear=True), refuse, \
                    patch.object(uart_bridge, "Path", lambda *a: _NoSysfs()):
                self.assertIsNone(uart_bridge.UARTBridge._find_device_by_serial(None, "0001"), value)

    def test_bridge_asks_the_host_when_switched(self):
        with patch.dict(os.environ, {"LAGER_HOSTLINK": "1"}), \
                patch.object(serial_proxy, "find_device_by_serial", lambda s: f"/dev/ttyUSB0:{s}"):
            self.assertEqual(uart_bridge.UARTBridge._find_device_by_serial(None, "0001"),
                             "/dev/ttyUSB0:0001")

    SITE_PROBE = "import serial\nprint(getattr(serial.Serial, '_lager_hostlink', False))\n"
    SITE = os.path.join(BOX, "lager", "hostlink", "site")

    def test_startup_hook_installs_only_when_switched(self):
        self.assertEqual(self._probe("1", self.SITE_PROBE, [self.SITE]), "True")
        self.assertEqual(self._probe(None, self.SITE_PROBE, [self.SITE]), "False")
        # Without the directory on the path -- every Linux Box -- nothing runs.
        self.assertEqual(self._probe("1", self.SITE_PROBE), "False")

    def test_startup_hook_does_not_import_lager(self):
        code = "import sys\nprint('lager' in sys.modules, 'requests' in sys.modules)\n"
        self.assertEqual(self._probe("1", code, [self.SITE]), "False False")


class _NoSysfs:
    def exists(self):
        return False


if __name__ == "__main__":
    unittest.main()
