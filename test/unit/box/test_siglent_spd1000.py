# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Siglent SPD1000X (SPD1168X) supply driver against a fake SCPI transport.

Pinned here:

* The exact SPD1000X command forms (``CH1:VOLTage``, ``MEASure:VOLTage? CH1``,
  ``OUTPut CH1,ON``), and that ``*OPC?`` is never sent.
* Every transaction is spaced ``COMMAND_INTERVAL_S`` after the previous one.
* Output state and CV/CC come from the ``SYSTem:STATus?`` hex bitfield, and a
  failed read is "unknown", not "off".
* OVP/OCP requests fail with ``SupplyBackendError`` before anything is written.
* Setpoints outside 0..model maximum are refused before anything is written.
* Dispatcher, hardware_service module and net mapper routing, and the
  scanner's f4ec:1410 entry with its single channel.

No hardware: the instrument is a dict of canned replies.
"""

import io
import itertools
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'serial', 'serial.tools', 'serial.tools.list_ports',
]:
    parts = _dep.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.exceptions import SupplyBackendError  # noqa: E402
from lager.power.supply import siglent_spd1000 as spd  # noqa: E402
from lager.power.supply.siglent_spd1000 import SiglentSPD1000X  # noqa: E402

IDN_1168 = "Siglent Technologies,SPD1168X,SPD13DCX4R0123,1.01.01.02.05,V3.0"
IDN_1305 = "Siglent Technologies,SPD1305X,SPD13DCQ4R0456,1.01.01.02.05,V3.0"


class FakeClock:
    """Stands in for ``time`` in the driver: sleeps advance a virtual clock."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeSPD:
    """A pyvisa-resource-shaped SPD1000X. Records writes, answers queries."""

    def __init__(self, clock, idn=IDN_1168):
        self.clock = clock
        self.replies = {
            "*IDN?": idn,
            "SYSTem:STATus?": "0x00",
            "SYSTem:ERRor?": "0  No Error",
            "CH1:VOLTage?": "5.000",
            "CH1:CURRent?": "1.000",
            "MEASure:VOLTage? CH1": "4.998",
            "MEASure:CURRent? CH1": "0.250",
            "MEASure:POWEr? CH1": "1.250",
        }
        self.log = []  # (time, kind, command)
        self.failing = set()

    def write(self, cmd):
        self.log.append((self.clock.now, "write", cmd))

    def query(self, cmd):
        self.log.append((self.clock.now, "query", cmd))
        if cmd in self.failing:
            raise OSError("VI_ERROR_TMO")
        if cmd not in self.replies:
            raise AssertionError(f"unexpected query {cmd!r}")
        return self.replies[cmd] + "\n"

    def close(self):
        pass

    def commands(self, kind=None):
        return [c for _, k, c in self.log if kind is None or k == kind]


class SPDTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = patch.object(spd, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, idn=IDN_1168):
        fake = FakeSPD(self.clock, idn)
        drv = SiglentSPD1000X(instr=fake)
        return drv, fake


class IdentityTests(SPDTestCase):
    def test_spd1168x_accepted_with_its_limits(self):
        drv, _ = self.make()
        self.assertEqual(drv.get_channel_limits(), {"voltage_max": 16.0, "current_max": 8.0})

    def test_spd1305x_uses_its_own_limits(self):
        drv, _ = self.make(IDN_1305)
        self.assertEqual(drv.get_channel_limits(), {"voltage_max": 30.0, "current_max": 5.0})

    def test_other_instrument_rejected(self):
        with self.assertRaises(SupplyBackendError):
            self.make("Siglent Technologies,SPD3303X-E,SPD3EE,1.0,V1")
        with self.assertRaises(SupplyBackendError):
            self.make("RIGOL TECHNOLOGIES,DP832,DP8B1,00.01.14")

    def test_channel_other_than_one_rejected(self):
        with self.assertRaises(SupplyBackendError):
            SiglentSPD1000X(instr=FakeSPD(self.clock), channel=2)

    def test_none_channel_means_one(self):
        drv = SiglentSPD1000X(instr=FakeSPD(self.clock), channel=None)
        self.assertEqual(drv.channel, 1)

    def test_address_or_resource_required(self):
        with self.assertRaises(SupplyBackendError):
            SiglentSPD1000X()


class CommandFormTests(SPDTestCase):
    def test_setpoint_writes(self):
        drv, fake = self.make()
        with redirect_stdout(io.StringIO()):
            drv.voltage(3.3)
            drv.current(0.5)
        self.assertEqual(fake.commands("write"), ["CH1:VOLTage 3.300", "CH1:CURRent 0.500"])

    def test_setpoint_reads(self):
        drv, _ = self.make()
        self.assertEqual(drv.get_channel_voltage(), 5.0)
        self.assertEqual(drv.get_channel_current(), 1.0)

    def test_measurements(self):
        drv, fake = self.make()
        self.assertEqual(drv.measure_voltage(), 4.998)
        self.assertEqual(drv.measure_current(), 0.25)
        self.assertEqual(drv.measure_power(), 1.25)
        self.assertEqual(
            fake.commands("query")[-3:],
            ["MEASure:VOLTage? CH1", "MEASure:CURRent? CH1", "MEASure:POWEr? CH1"])

    def test_output_toggle(self):
        drv, fake = self.make()
        drv.enable()
        drv.disable()
        self.assertEqual(fake.commands("write"), ["OUTPut CH1,ON", "OUTPut CH1,OFF"])
        self.assertEqual(self.clock.sleeps.count(spd.OUTPUT_SETTLE_S), 2)

    def test_negative_setpoints_refused_without_a_write(self):
        drv, fake = self.make()
        with self.assertRaises(SupplyBackendError):
            drv.voltage(-1.0)
        with self.assertRaises(SupplyBackendError):
            drv.current(-0.1)
        self.assertEqual(fake.commands("write"), [])

    def test_setpoints_above_the_model_limit_refused_without_a_write(self):
        # The SPD drops these silently, so the driver must refuse them.
        drv, fake = self.make()
        with self.assertRaises(SupplyBackendError):
            drv.set_channel_voltage(16.001)
        with self.assertRaises(SupplyBackendError):
            drv.set_channel_current(8.5)
        self.assertEqual(fake.commands("write"), [])

    def test_setpoints_at_the_model_limit_accepted(self):
        drv, fake = self.make(IDN_1305)
        drv.set_channel_voltage(30.0)
        drv.set_channel_current(5.0)
        self.assertEqual(fake.commands("write"), ["CH1:VOLTage 30.000", "CH1:CURRent 5.000"])

    def test_non_numeric_reply_raises(self):
        drv, fake = self.make()
        fake.replies["MEASure:VOLTage? CH1"] = "garbage"
        with self.assertRaises(SupplyBackendError):
            drv.measure_voltage()

    def test_opc_never_sent(self):
        drv, fake = self.make()
        with redirect_stdout(io.StringIO()):
            drv.voltage(5.0)
            drv.enable()
            drv.get_full_state()
            drv.disable()
        self.assertFalse([c for c in fake.commands() if "OPC" in c.upper()])


class PacingTests(SPDTestCase):
    def test_transactions_spaced_by_interval(self):
        drv, fake = self.make()
        with redirect_stdout(io.StringIO()):
            drv.voltage(1.0)
            drv.current(1.0)
            drv.measure_voltage()
            drv.get_full_state()
        times = [t for t, _, _ in fake.log]
        self.assertGreater(len(times), 5)
        for prev, cur in itertools.pairwise(times):
            self.assertGreaterEqual(cur - prev, spd.COMMAND_INTERVAL_S - 1e-9)

    def test_liveness_probe_is_paced(self):
        # SupplyNet.get_monitor_state queries *IDN? through self.instr directly.
        drv, fake = self.make()
        drv.instr.query("*IDN?", check_errors=False)
        drv.instr.query("*IDN?", check_errors=False)
        t1, t2 = fake.log[-2][0], fake.log[-1][0]
        self.assertGreaterEqual(t2 - t1, spd.COMMAND_INTERVAL_S - 1e-9)

    def test_no_wait_after_idle(self):
        drv, _ = self.make()
        self.clock.now += 10
        before = len(self.clock.sleeps)
        drv.measure_current()
        self.assertEqual(len(self.clock.sleeps), before)


class StatusTests(SPDTestCase):
    CASES = (
        ("0x00", False, "CV"),
        ("0x10", True, "CV"),
        ("0x11", True, "CC"),
        ("0x01", False, "CC"),
        ("30", True, "CV"),     # bare hex, output on + 4-wire
        ("0x0130", True, "CV"),  # waveform display + 4-wire + on
    )

    def test_status_bits(self):
        drv, fake = self.make()
        for reply, on, mode in self.CASES:
            with self.subTest(reply=reply):
                fake.replies["SYSTem:STATus?"] = reply
                self.assertIs(drv.output_state(), on)
                self.assertIs(drv.output_is_enabled(), on)
                self.assertEqual(drv.get_output_mode(), mode)

    def test_failed_or_garbled_status_is_unknown(self):
        drv, fake = self.make()
        for reply in ("", "n/a", "ON"):
            with self.subTest(reply=reply):
                fake.replies["SYSTem:STATus?"] = reply
                self.assertIsNone(drv.output_state())
                self.assertFalse(drv.output_is_enabled())
                with self.assertRaises(SupplyBackendError):
                    drv.get_output_mode()
        fake.failing.add("SYSTem:STATus?")
        self.assertIsNone(drv.output_state())

    def test_parse_status(self):
        self.assertEqual(spd.parse_status("0x10"), 16)
        self.assertEqual(spd.parse_status(" 11\n"), 17)
        self.assertIsNone(spd.parse_status("xyz"))
        self.assertIsNone(spd.parse_status(None))


class ErrorQueueTests(SPDTestCase):
    def test_parse_error_reply(self):
        self.assertEqual(spd.parse_error_reply("0  No Error"), (0, "No Error"))
        self.assertEqual(spd.parse_error_reply('0,"No error"'), (0, "No error"))
        self.assertEqual(spd.parse_error_reply('-113,"Undefined header"'),
                         (-113, "Undefined header"))
        self.assertEqual(spd.parse_error_reply("23  Data out of range"),
                         (23, "Data out of range"))
        self.assertEqual(spd.parse_error_reply("garbage"), (-1, "garbage"))

    def test_check_error(self):
        drv, fake = self.make()
        drv.check_error()
        fake.replies["SYSTem:ERRor?"] = "23  Data out of range"
        with self.assertRaisesRegex(SupplyBackendError, "23"):
            drv.check_error()


class ProtectionTests(SPDTestCase):
    def test_protection_methods_raise(self):
        drv, _ = self.make()
        calls = [
            lambda: drv.ocp(1.0), lambda: drv.ocp(), lambda: drv.ovp(5.0), lambda: drv.ovp(),
            drv.clear_ocp, drv.clear_ovp,
            lambda: drv.set_overcurrent_protection_value(1.0),
            drv.get_overcurrent_protection_value,
            drv.overcurrent_protection_is_tripped,
            lambda: drv.set_overvoltage_protection_value(5.0),
            drv.get_overvoltage_protection_value,
            drv.overvoltage_protection_is_tripped,
        ]
        for call in calls:
            with self.assertRaisesRegex(SupplyBackendError, "not supported"):
                call()

    def test_setpoint_with_protection_writes_nothing(self):
        drv, fake = self.make()
        with self.assertRaisesRegex(SupplyBackendError, "OVP"):
            drv.voltage(5.0, ovp=5.5)
        with self.assertRaisesRegex(SupplyBackendError, "OCP"):
            drv.current(1.0, ocp=1.2)
        self.assertEqual(fake.commands("write"), [])


class StateReportTests(SPDTestCase):
    def test_monitor_state(self):
        drv, fake = self.make()
        fake.replies["SYSTem:STATus?"] = "0x11"
        state = drv.get_monitor_state(1)
        self.assertEqual(state["voltage"], 4.998)
        self.assertEqual(state["current"], 0.25)
        self.assertEqual(state["power"], 1.25)
        self.assertIs(state["enabled"], True)
        self.assertEqual(state["mode"], "CC")
        self.assertEqual(state["voltage_set"], 5.0)
        self.assertEqual(state["current_set"], 1.0)
        self.assertEqual(state["voltage_max"], 16.0)
        self.assertEqual(state["current_max"], 8.0)
        for key in ("ocp_limit", "ocp_tripped", "ovp_limit", "ovp_tripped"):
            self.assertIsNone(state[key], key)

    def test_failed_measurement_is_none_not_zero(self):
        drv, fake = self.make()
        fake.failing.add("MEASure:VOLTage? CH1")
        self.assertIsNone(drv.get_monitor_state(1)["voltage"])

    def test_full_state_text(self):
        drv, fake = self.make()
        fake.replies["SYSTem:STATus?"] = "0x10"
        fake.failing.add("MEASure:CURRent? CH1")
        out = io.StringIO()
        with redirect_stdout(out):
            drv.get_full_state()
        text = out.getvalue()
        self.assertIn("Enabled: ON", text)
        self.assertIn("Voltage: 4.998", text)
        self.assertIn("Current: n/a", text)
        self.assertIn("OCP Limit: unsupported", text)
        self.assertIn("OVP Limit: unsupported", text)
        self.assertIn("Voltage_Max: 16.0", text)

    def test_output_off_reports_measured_values_not_setpoints(self):
        drv, fake = self.make()
        fake.replies["SYSTem:STATus?"] = "0x00"
        fake.replies["MEASure:VOLTage? CH1"] = "0.002"
        fake.replies["MEASure:CURRent? CH1"] = "0.000"
        fake.replies["MEASure:POWEr? CH1"] = "0.000"
        out = io.StringIO()
        with redirect_stdout(out):
            drv.state()
        text = out.getvalue()
        self.assertIn("Enabled: OFF", text)
        self.assertIn("Voltage: 0.002", text)
        self.assertIn("Current: 0.0", text)
        self.assertNotIn("Voltage: 5.0", text)
        self.assertNotIn("Current: 1.0", text)

    def test_state_with_unknown_output(self):
        drv, fake = self.make()
        fake.failing.add("SYSTem:STATus?")
        out = io.StringIO()
        with redirect_stdout(out):
            drv.state()
        self.assertIn("Enabled: unknown", out.getvalue())
        self.assertIn("Mode: unknown", out.getvalue())


class OpenTests(SPDTestCase):
    def test_address_opens_with_lf_termination(self):
        raw = FakeSPD(self.clock)
        rm = MagicMock()
        rm.open_resource.return_value = raw
        with patch.object(spd, "pyvisa") as pv:
            pv.ResourceManager.return_value = rm
            drv = SiglentSPD1000X(address="TCPIP::192.168.1.50::INSTR")
        rm.open_resource.assert_called_once_with("TCPIP::192.168.1.50::INSTR")
        self.assertEqual(raw.write_termination, "\n")
        self.assertEqual(raw.read_termination, "\n")
        self.assertIs(drv.instr.instr, raw)

    def test_create_device_factory(self):
        raw = FakeSPD(self.clock)
        rm = MagicMock()
        rm.open_resource.return_value = raw
        with patch.object(spd, "pyvisa") as pv:
            pv.ResourceManager.return_value = rm
            drv = spd.create_device({"address": "USB0::0xF4EC::0x1410::SPD13X::INSTR",
                                     "channel": 1, "instrument": "Siglent_SPD1168X"})
        self.assertIsInstance(drv, SiglentSPD1000X)


class RoutingTests(unittest.TestCase):
    def test_supply_dispatcher_chooses_driver(self):
        from lager.power.supply.dispatcher import SupplyDispatcher
        d = SupplyDispatcher()
        for name in ("Siglent_SPD1168X", "siglent spd1168x", "SIGLENT-SPD1305X"):
            with self.subTest(name=name):
                self.assertIs(d._choose_driver(name), SiglentSPD1000X)

    def test_hardware_service_module_name(self):
        from lager.dispatchers import helpers
        self.assertEqual(helpers._supply_module_for_instrument("Siglent_SPD1168X"),
                         "siglent_spd1000")

    def test_net_mapper_uses_dp800_function_mapper(self):
        from lager.nets import net as net_mod
        from lager.nets.mappers.rigol_dp800 import RigolDP800FunctionMapper
        fake_net = MagicMock()
        mapper = net_mod.mapper_factory(
            fake_net, "USB0::0xF4EC::0x1410::SPD13X::INSTR",
            {"instrument": "Siglent_SPD1168X", "address": "USB0::0xF4EC::0x1410::SPD13X::INSTR"})
        self.assertIsInstance(mapper, RigolDP800FunctionMapper)
        self.assertEqual(mapper.device.device_name, "siglent_spd1000")

    def test_scanner_catalogs_f4ec_1410_as_one_channel_supply(self):
        # f4ec:1410 read from lsusb on a real SPD1168X (iProduct "SPD1000X").
        import importlib.util
        path = os.path.join(_BOX_ROOT, "lager", "http_handlers", "usb_scanner.py")
        spec = importlib.util.spec_from_file_location("usb_scanner_spd_under_test", path)
        scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner)
        entry = scanner.SUPPORTED_USB["Siglent_SPD1168X"]
        self.assertEqual((entry["vid"], entry["pid"]), ("f4ec", "1410"))
        self.assertEqual(entry["net_type"], ["power-supply"])
        self.assertEqual(scanner.CHANNEL_MAPS["Siglent_SPD1168X"], {"power-supply": ["1"]})
        self.assertEqual(scanner._VIDPID_TO_NAME[("f4ec", "1410")], "Siglent_SPD1168X")


if __name__ == "__main__":
    unittest.main()
