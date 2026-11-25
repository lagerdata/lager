# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Serial ports opened by drivers running in the macOS host agent.

A serial supply (Rigol DP700 over VISA ASRL, EA PSB) and the DexArm run in
the agent, whose drivers name ports as on a Linux Box (``/dev/ttyUSB0``)
while the Mac's are ``/dev/cu.*``. Covers the agent's ``serial.Serial``
translating the Linux name on open, the ``serial_id`` lookups re-reading the
bus first, and the port search the EA drivers share with a Linux Box.

A pseudo-terminal stands in for the Mac device, so the opens are real and the
suite runs on Linux and macOS alike. No hardware or ``ioreg`` is used.
"""
import glob
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import serial

from lager.devices import serial_id
from lager.host import darwin_scan, darwin_serial


def _pty():
    """A pseudo-terminal: (master fd, path of the side pySerial opens)."""
    master, slave = os.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    return master, path


class _AgentSerial(unittest.TestCase):
    """``serial.Serial`` as the agent installs it; restored afterwards."""

    def setUp(self):
        restore = patch.object(serial, "Serial", serial.Serial)
        restore.start()
        self.addCleanup(restore.stop)
        self.master, self.device = _pty()
        self.addCleanup(os.close, self.master)
        self.names = darwin_serial.Names()
        # A name without "usbmodem" is an adapter: ttyUSB0.
        self.names.sync([self.device])
        self.refreshes = 0
        self.refresh_fails = False
        darwin_serial.install_pyserial(refresh=self._refresh, names=self.names)

    def _refresh(self):
        self.refreshes += 1
        if self.refresh_fails:
            raise OSError("ioreg failed")


class LinuxNameOpenTests(_AgentSerial):
    def test_linux_name_opens_the_mac_device(self):
        port = serial.Serial("/dev/ttyUSB0", timeout=1)
        self.addCleanup(port.close)
        os.write(self.master, b"*IDN?\n")
        self.assertEqual(port.readline(), b"*IDN?\n")
        port.write(b"ok\n")
        self.assertEqual(os.read(self.master, 16), b"ok\n")

    def test_port_keeps_its_linux_name(self):
        port = serial.Serial("/dev/ttyUSB0")
        self.addCleanup(port.close)
        self.assertEqual((port.port, port.portstr, port.name), ("/dev/ttyUSB0",) * 3)

    def test_bus_is_read_again_before_each_open(self):
        for _ in range(2):
            serial.Serial("/dev/ttyUSB0").close()
        self.assertEqual(self.refreshes, 2)

    def test_port_set_after_construction_is_translated(self):
        # serial_for_url (which pyvisa-py uses) builds the port unopened.
        port = serial.serial_for_url("/dev/ttyUSB0", timeout=1)
        self.addCleanup(port.close)
        self.assertTrue(port.is_open)

    def test_reopen_after_close_finds_the_device_again(self):
        port = serial.Serial("/dev/ttyUSB0")
        port.close()
        port.open()
        self.addCleanup(port.close)
        self.assertTrue(port.is_open)

    def test_missing_linux_name_fails_as_on_linux(self):
        with self.assertRaises(serial.SerialException) as caught:
            serial.Serial("/dev/ttyUSB7")
        self.assertEqual(caught.exception.errno, 2)
        self.assertIn("/dev/ttyUSB7", str(caught.exception))

    def test_errors_name_the_linux_port_not_the_mac_one(self):
        held = serial.Serial("/dev/ttyUSB0", exclusive=True)
        self.addCleanup(held.close)
        with self.assertRaises(serial.SerialException) as caught:
            serial.Serial("/dev/ttyUSB0", exclusive=True)
        self.assertIn("/dev/ttyUSB0", str(caught.exception))
        self.assertNotIn(self.device, str(caught.exception))

    def test_other_paths_open_unchanged_without_a_bus_read(self):
        port = serial.Serial(self.device)
        self.addCleanup(port.close)
        self.assertEqual(port.portstr, self.device)
        self.assertEqual(self.refreshes, 0)

    def test_a_failing_bus_read_falls_back_to_the_last_table(self):
        self.refresh_fails = True
        port = serial.Serial("/dev/ttyUSB0")
        self.addCleanup(port.close)
        self.assertTrue(port.is_open)

    def test_installing_twice_wraps_once(self):
        installed = serial.Serial
        darwin_serial.install_pyserial(refresh=self._refresh, names=self.names)
        self.assertIs(serial.Serial, installed)


class VisaAsrlTests(_AgentSerial):
    """A Rigol DP700's resource string, opened by pyvisa-py as the agent does."""

    def setUp(self):
        super().setUp()
        # Other suites leave stand-ins for pyvisa in sys.modules; load the
        # real one for this test and put theirs back afterwards.
        modules = patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        for name in [n for n in sys.modules if n.split(".")[0] in ("pyvisa", "pyvisa_py")]:
            del sys.modules[name]

    def test_asrl_resource_with_a_linux_name_reaches_the_device(self):
        import pyvisa

        rm = pyvisa.ResourceManager("@py")
        self.addCleanup(rm.close)
        inst = rm.open_resource("ASRL/dev/ttyUSB0::INSTR", read_termination="\n",
                                write_termination="\n", timeout=2000)
        self.addCleanup(inst.close)
        inst.write("*IDN?")
        self.assertEqual(os.read(self.master, 16), b"*IDN?\n")
        os.write(self.master, b"RIGOL TECHNOLOGIES,DP711\n")
        self.assertEqual(inst.read(), "RIGOL TECHNOLOGIES,DP711")


class GlobPortsTests(unittest.TestCase):
    def setUp(self):
        self.names = darwin_serial.Names()
        self.names.sync(["/dev/cu.usbserial-A", "/dev/cu.usbmodem1", "/dev/cu.usbserial-B"])

    def test_patterns_match_the_linux_names(self):
        self.assertEqual(darwin_serial.glob_ports("/dev/ttyUSB*", self.names),
                         ["/dev/ttyUSB0", "/dev/ttyUSB1"])
        self.assertEqual(darwin_serial.glob_ports("/dev/ttyACM*", self.names), ["/dev/ttyACM0"])

    def test_mac_names_and_udev_links_match_nothing(self):
        for pattern in ("/dev/cu.*", "/dev/serial/by-id/*", "/dev/serial/by-id/*PSB*"):
            self.assertEqual(darwin_serial.glob_ports(pattern, self.names), [], pattern)


class LinuxGlobPortsTests(unittest.TestCase):
    """On a Linux Box ``serial_id.glob_ports`` is the glob the drivers used."""

    def test_same_answer_as_glob(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("ttyACM1", "ttyACM0", "ttyUSB0", "ttyS0"):
                open(os.path.join(tmp, name), "w").close()
            for pattern in ("ttyACM*", "ttyUSB*", "tty*", "none*"):
                pattern = os.path.join(tmp, pattern)
                self.assertEqual(serial_id.glob_ports(pattern), sorted(glob.glob(pattern)))


class LinuxPortExistsTests(unittest.TestCase):
    """On a Linux Box ``serial_id.port_exists`` is ``os.path.exists``."""

    def test_same_answer_as_os_path_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            present = os.path.join(tmp, "ttyUSB0")
            open(present, "w").close()
            for path in (present, os.path.join(tmp, "ttyUSB1")):
                self.assertEqual(serial_id.port_exists(path), os.path.exists(path))


class MacPortExistsTests(unittest.TestCase):
    def test_linux_names_by_the_table_other_paths_on_disk(self):
        names = darwin_serial.Names()
        names.sync(["/dev/cu.usbserial-A"])
        self.assertTrue(darwin_serial.port_exists("/dev/ttyUSB0", names))
        self.assertFalse(darwin_serial.port_exists("/dev/ttyUSB1", names))
        with tempfile.NamedTemporaryFile() as fh:
            self.assertTrue(darwin_serial.port_exists(fh.name, names))


class EaPortSearchTests(unittest.TestCase):
    """Both EA drivers search through ``serial_id.glob_ports``."""

    def _drivers(self):
        from lager.power.solar import ea as solar_ea
        from lager.power.supply import ea as supply_ea

        return (("supply", supply_ea), ("solar", solar_ea))

    def test_first_acm_port_when_no_udev_link(self):
        answers = {"/dev/ttyACM*": ["/dev/ttyACM0", "/dev/ttyACM1"]}
        with patch.object(serial_id, "glob_ports", lambda p: answers.get(p, [])):
            for label, module in self._drivers():
                self.assertEqual(module._find_serial_device_path(None), "/dev/ttyACM0", label)

    def test_falls_back_to_usb_adapters(self):
        answers = {"/dev/ttyUSB*": ["/dev/ttyUSB3"]}
        with patch.object(serial_id, "glob_ports", lambda p: answers.get(p, [])):
            for label, module in self._drivers():
                self.assertEqual(module._find_serial_device_path("ABC"), "/dev/ttyUSB3", label)

    def test_nothing_attached(self):
        with patch.object(serial_id, "glob_ports", lambda p: []):
            for label, module in self._drivers():
                self.assertIsNone(module._find_serial_device_path(None), label)


class FreshLookupTests(unittest.TestCase):
    """The agent's ``serial_id`` lookups re-read the bus before answering."""

    def setUp(self):
        for name in darwin_scan._FRESH_LOOKUPS:
            patcher = patch.object(serial_id, name, getattr(serial_id, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reads = 0

        def read():
            self.reads += 1

        refresh = patch.object(darwin_scan, "_refresh_locked", read)
        refresh.start()
        self.addCleanup(refresh.stop)
        darwin_scan.install_fresh_lookups()

    def test_each_lookup_reads_the_bus_first(self):
        with patch.object(serial_id, "_SYS_TTY", serial_id.Path("/nonexistent/tty")):
            serial_id.list_cables()
            serial_id.resolve_identity({"vid": "067b", "pid": "23a3", "serial": "1"})
            serial_id.identity_for_tty("/dev/ttyUSB0")
        self.assertEqual(self.reads, 3)

    def test_a_serial_address_resolves_against_a_fresh_bus(self):
        # The DP700's path: resolve_address_to_tty -> resolve_tty.
        with patch.object(serial_id, "_SYS_TTY", serial_id.Path("/nonexistent/tty")):
            self.assertIsNone(serial_id.resolve_address_to_tty(
                "serial://067b:23a3/serial/00000006"))
        self.assertEqual(self.reads, 1)

    def test_glob_answers_from_the_name_table(self):
        names = darwin_serial.Names()
        names.sync(["/dev/cu.usbmodem9"])
        with patch.object(darwin_serial, "NAMES", names):
            self.assertEqual(serial_id.glob_ports("/dev/ttyACM*"), ["/dev/ttyACM0"])
        self.assertEqual(self.reads, 1)

    def test_installing_twice_wraps_once(self):
        darwin_scan.install_fresh_lookups()
        serial_id.list_cables()
        self.assertEqual(self.reads, 1)

    def test_a_failing_bus_read_still_answers(self):
        def broken():
            raise OSError("ioreg failed")

        with patch.object(darwin_scan, "_refresh_locked", broken), \
                patch.object(serial_id, "_SYS_TTY", serial_id.Path("/nonexistent/tty")):
            self.assertEqual(serial_id.list_cables(), [])


if __name__ == "__main__":
    unittest.main()
