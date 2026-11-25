# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Serial instruments driven from the macOS host agent, against simulated ones.

The Rigol DP700, EA PSB and DexArm drivers run unedited in the agent. Each
test sets the agent up as it starts on a Mac (the sysfs-shaped tree, the
refresh-first ``serial_id`` lookups, the Linux-name ``serial.Serial``), puts a
pseudo-terminal where the Mac's ``/dev/cu.*`` device would be, and answers on
it the way the instrument does. The bus "scan" is a fixture, so no ``ioreg``
and no hardware are involved, and the suite runs on Linux and macOS alike.

What this does not prove: that a real instrument's replies match the
simulated ones. That is the drivers' own concern on a Linux Box too.
"""
import os
import re
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import serial

from lager.devices import serial_id
from lager.host import darwin_scan, darwin_serial
from lager.protocols.uart import uart_bridge

PROLIFIC = (0x067B, 0x23A3)
LOCATION = 0x01120000  # bus 1, port 1.2


def _pty():
    """(master fd, slave fd, slave path). The test holds the slave open for
    its whole run: with no slave open, a read of the master ends at once."""
    master, slave = os.openpty()
    return master, slave, os.ttyname(slave)


class _Instrument(threading.Thread):
    """Answers commands arriving on a pty master, one line at a time."""

    def __init__(self, master, answer, eol=b"\n"):
        super().__init__(daemon=True)
        self.master = master
        self.answer = answer
        self.eol = eol
        self.received = []
        self._done = threading.Event()

    def run(self):
        buffer = b""
        while not self._done.is_set():
            try:
                chunk = os.read(self.master, 1024)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while True:
                match = re.search(rb"\r\n|\n|\r", buffer)
                if not match:
                    break
                line, buffer = buffer[:match.start()], buffer[match.end():]
                command = line.decode(errors="replace").strip()
                if not command:
                    continue
                self.received.append(command)
                for reply in self.answer(command) or ():
                    os.write(self.master, reply.encode() + self.eol)

    def stop(self):
        self._done.set()


class _AgentWithOnePort(unittest.TestCase):
    """The agent as installed on a Mac, with one USB-serial device on the bus."""

    vid_pid = PROLIFIC
    usb_serial = "00000006"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Everything the agent's install replaces, put back afterwards.
        for target, name in ([(serial, "Serial"), (darwin_serial, "NAMES"),
                              (darwin_scan, "_root"), (darwin_scan, "_refresh_locked"),
                              (serial_id, "_SYS_TTY"), (uart_bridge, "Path")]
                             + [(serial_id, n) for n in darwin_scan._FRESH_LOOKUPS]):
            patcher = patch.object(target, name, getattr(target, name))
            patcher.start()
            self.addCleanup(patcher.stop)

        self.master, slave, self.device = _pty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, slave)
        self.attached = True
        self.scans = 0
        darwin_serial.NAMES = darwin_serial.Names()
        darwin_scan._root = None
        darwin_scan._refresh_locked = self._scan

        darwin_scan.install_serial(os.path.join(self.tmp.name, "sysfs"))
        darwin_scan.install_fresh_lookups()
        darwin_serial.install_pyserial(refresh=darwin_scan.refresh)

    def _scan(self):
        """What reading the I/O Registry finds now."""
        self.scans += 1
        vid, pid = self.vid_pid
        devices, ports = [], []
        if self.attached:
            devices = [{"sysfs_name": f"{LOCATION:08x}", "vid": f"{vid:04x}",
                        "pid": f"{pid:04x}", "serial": self.usb_serial,
                        "product": "USB-Serial Controller", "manufacturer": "Prolific"}]
            ports = [{"device": self.device, "location": LOCATION, "interface": 0}]
        names = darwin_serial.NAMES.sync(p["device"] for p in ports)
        darwin_scan.render(devices, darwin_scan._root, ports, names)

    def start_instrument(self, answer, eol=b"\n"):
        instrument = _Instrument(self.master, answer, eol)
        instrument.start()
        self.addCleanup(instrument.stop)
        return instrument

    def real_pyvisa(self, *modules):
        """The real pyvisa, for this test, in the named driver modules.

        Other suites leave stand-ins for pyvisa in ``sys.modules``.
        """
        saved = patch.dict(sys.modules)
        saved.start()
        self.addCleanup(saved.stop)
        for name in [n for n in sys.modules if n.split(".")[0] in ("pyvisa", "pyvisa_py")]:
            del sys.modules[name]
        import pyvisa

        for module in modules:
            patcher = patch.object(module, "pyvisa", pyvisa)
            patcher.start()
            self.addCleanup(patcher.stop)
        return pyvisa


class _Dp711:
    """Enough of a Rigol DP711's SCPI for the driver's supply commands."""

    def __init__(self):
        self.volts, self.amps, self.on = 0.0, 1.0, False

    def __call__(self, command):
        upper = command.upper()
        if upper == "*IDN?":
            return ["RIGOL TECHNOLOGIES,DP711,DP7A000000001,00.01.05"]
        if upper.startswith(":VOLT "):
            self.volts = float(command.split()[1])
        elif upper.startswith(":CURR "):
            self.amps = float(command.split()[1])
        elif upper.startswith(":OUTP "):
            self.on = upper.endswith("ON")
        elif upper == ":VOLT?":
            return [f"{self.volts:.3f}"]
        elif upper == ":CURR?":
            return [f"{self.amps:.3f}"]
        elif upper == ":OUTP?":
            return ["ON" if self.on else "OFF"]
        elif upper == ":MEAS:VOLT?":
            return [f"{self.volts if self.on else 0.0:.3f}"]
        elif upper.endswith("?"):
            return ["0"]
        return []


class RigolDp700Tests(_AgentWithOnePort):
    def setUp(self):
        super().setUp()
        from lager.power.supply import rigol_dp700

        self.module = rigol_dp700
        self.real_pyvisa(rigol_dp700)
        # No assignment store: the catalog's serial settings apply.
        patcher = patch.object(rigol_dp700, "_assignment_serial_settings", lambda a: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.supply = _Dp711()
        self.instrument = self.start_instrument(self.supply)

    def _open(self, address):
        driver = self.module.RigolDP700(address=address)
        self.addCleanup(driver._close)
        return driver

    def test_serial_address_drives_the_supply(self):
        driver = self._open(f"serial://067b:23a3/serial/{self.usb_serial}")
        driver.set_channel_voltage(3.3)
        driver.set_channel_current(0.5)
        driver.enable_output()
        self.assertAlmostEqual(driver.get_channel_voltage(), 3.3)
        self.assertAlmostEqual(driver.get_channel_current(), 0.5)
        self.assertIs(driver.output_state(), True)
        self.assertAlmostEqual(driver.measure_voltage(), 3.3)
        driver.disable_output()
        self.assertIs(driver.output_state(), False)
        self.assertEqual(driver._opened_tty, "/dev/ttyUSB0")
        self.assertIn(":VOLT 3.3", self.instrument.received)

    def test_port_is_opened_once_while_the_cable_stays(self):
        driver = self._open(f"serial://067b:23a3/serial/{self.usb_serial}")
        opened = []
        real_reopen = driver._reopen
        driver._reopen = lambda: opened.append(1) or real_reopen()
        for _ in range(3):
            driver.measure_voltage()
        self.assertTrue(driver._is_connection_alive())
        self.assertEqual(opened, [])

    def test_unplugged_cable_reads_as_a_dead_connection(self):
        driver = self._open(f"serial://067b:23a3/serial/{self.usb_serial}")
        self.attached = False
        self.assertFalse(driver._is_connection_alive())
        self.attached = True
        self.assertTrue(driver._is_connection_alive())

    def test_address_saved_on_a_linux_box_as_a_tty(self):
        for address in ("/dev/ttyUSB0", "ASRL/dev/ttyUSB0::INSTR"):
            driver = self._open(address)
            driver.set_channel_voltage(1.8)
            self.assertAlmostEqual(driver.get_channel_voltage(), 1.8, msg=address)
            driver._close()

    def test_cable_that_is_not_attached(self):
        self.attached = False
        with self.assertRaises(self.module.DeviceNotFoundError):
            self._open(f"serial://067b:23a3/serial/{self.usb_serial}")


class EaPsbTests(_AgentWithOnePort):
    """An EA PSB's USB-TMC address, reached through its serial (CDC) port."""

    vid_pid = (0x232E, 0x0010)
    usb_serial = "2215040001"

    def _ea(self, command):
        upper = command.upper()
        if upper == "*IDN?":
            return [f"EA Elektro-Automatik,PSB 10060-60,{self.usb_serial},V2.10"]
        if upper.startswith("SYST:ERR") or upper.startswith("SYSTEM:ERROR"):
            return ['0,"No error"']
        if upper.endswith("?"):
            return ["0"]
        return []

    def setUp(self):
        super().setUp()
        from lager.power.supply import ea

        self.module = ea
        pyvisa = self.real_pyvisa(ea)
        # The USB-TMC attempts fail here as they do on a Mac, where the EA's
        # port is a CDC serial device. Only ASRL resources reach pyvisa.
        real_manager = pyvisa.ResourceManager

        def manager(*args):
            rm = real_manager("@py")
            real_open = rm.open_resource

            def open_resource(address, **kwargs):
                if not address.startswith("ASRL"):
                    raise pyvisa.errors.VisaIOError(-1073807343)  # resource not found
                return real_open(address, **kwargs)

            rm.open_resource = open_resource
            return rm

        patcher = patch.object(pyvisa, "ResourceManager", manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.instrument = self.start_instrument(self._ea)

    def test_usbtmc_address_falls_back_to_the_serial_port(self):
        driver = self.module.EA(address=f"USB0::0x232E::0x0010::{self.usb_serial}::INSTR")
        self.addCleanup(lambda: driver.instr.instr.close()
                        if hasattr(driver.instr, "instr") else driver.instr.close())
        self.assertIn("*IDN?", self.instrument.received)
        self.assertIn("SYSTem:LOCK ON", self.instrument.received)


class DexarmTests(_AgentWithOnePort):
    vid_pid = (0x0483, 0x5740)
    usb_serial = "3A7C35603333"

    def _arm(self, command):
        if command == "M2010":
            return ["Firmware V3.1.0", "ok"]
        if command == "M114":
            return ["X:0.00 Y:300.00 Z:0.00 E:0.00", "DEXARM Theta A:0.00 B:0.00 C:0.00", "ok"]
        return ["ok"]

    def setUp(self):
        super().setUp()
        from lager.automation.arm import rotrics

        self.module = rotrics
        self.instrument = self.start_instrument(self._arm, eol=b"\r\n")

    def _check(self, arm):
        self.addCleanup(arm.close)
        self.assertEqual(arm.firmware_version, (3, 1, 0))
        self.assertEqual(arm.position(), (0.0, 300.0, 0.0))

    def test_port_saved_on_a_linux_box(self):
        self._check(self.module.Dexarm(port="/dev/ttyUSB0"))

    def test_found_by_usb_serial_among_the_macs_ports(self):
        class _Port:
            device = self.device
            serial_number = self.usb_serial
            vid, pid = self.vid_pid

        with patch.object(self.module.list_ports, "comports", lambda: [_Port()]):
            self._check(self.module.Dexarm(serial_number=self.usb_serial))


if __name__ == "__main__":
    unittest.main()
