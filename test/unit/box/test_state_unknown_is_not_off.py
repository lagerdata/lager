# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
A state report must be able to say "not known", and must never say "off" for it.

Output-state reads were folded into a bool at every layer: drivers turned a
failed or garbled ``:OUTP?`` into ``False``, the ``/nets/state`` briefs turned
``None`` into ``"off"``, and YKUSH turned pykush's error value (255) into
``True``. Each of these shows a user a confident, wrong state. Pinned here:

* ``parse_on_off`` is strict: ``1``/``0``/``ON``/``OFF`` (any case, padded) or
  ``None``.
* Each driver's reporting read (``output_state`` and friends) yields ``None``
  for a failed or unrecognised reply, while ``output_is_enabled`` keeps its
  plain-bool contract for control flow.
* ``/nets/state`` shows ``?`` in the on/off slot, carries a structured
  ``enabled`` only when it is known, and shows unread measurements as ``?``.
* An integer pin of 0 reaches the LabJack batch read as pin 0.
* Two nets on one hub port both get the port's state.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'pigpio', 'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope', 'brainstem',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
    'flask_socketio', 'hid',
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

from lager.util.on_off import parse_on_off  # noqa: E402
from lager.power.supply import supply_net as supply_net_mod  # noqa: E402
from lager.power.supply.rigol_dp800 import RigolDP800  # noqa: E402
from lager.power.supply.rigol_dp700 import RigolDP700  # noqa: E402
from lager.power.supply.ea import EA  # noqa: E402
from lager.power.supply.keithley import Keithley2281S  # noqa: E402
from lager.power.battery.keithley import KeithleyBattery  # noqa: E402
from lager.power.eload.rigol_dl3021 import RigolDL3021  # noqa: E402
from lager.automation.usb_hub import ykush as ykush_mod  # noqa: E402
from lager.automation.usb_hub import dispatcher as usb_dispatcher  # noqa: E402
from lager.automation.usb_hub.usb_net import PortStateError  # noqa: E402
from lager.http_handlers import nets_handler  # noqa: E402
from lager import hardware_service  # noqa: E402


# Replies an instrument can give to an output-state query, and what they mean.
REPLIES = [
    ("1", True), ("0", False), ("ON", True), ("OFF", False),
    ("on", True), ("off", False), (" ON\n", True), ("0\r\n", False),
    ("", None), ("n/a", None), ("-113,Undefined header", None),
    ("2", None), ("ONN", None), (None, None),
]


class ParseOnOffTests(unittest.TestCase):
    def test_replies(self):
        for raw, expected in REPLIES:
            with self.subTest(raw=raw):
                self.assertIs(parse_on_off(raw), expected)


def _bare(cls):
    """A driver instance with no connection, for method-level tests."""
    return object.__new__(cls)


class SupplyOutputStateTests(unittest.TestCase):
    """Each supply's reporting read says None for a reply that is not an
    answer; ``output_is_enabled`` still gives False for control flow."""

    def _check(self, drv, set_reply):
        for raw, expected in REPLIES:
            with self.subTest(driver=type(drv).__name__, raw=raw):
                set_reply(raw)
                self.assertIs(drv.output_state(), expected)
                self.assertIs(drv.output_is_enabled(), expected is True)

    def test_dp700(self):
        drv = _bare(RigolDP700)
        reply = {}
        # _safe_query returns its default on failure; None stands for that.
        drv._safe_query = lambda cmd, default="n/a": (
            default if reply["v"] is None else reply["v"])
        self._check(drv, lambda raw: reply.update(v=raw))

    def test_ea(self):
        drv = _bare(EA)
        reply = {}
        drv._safe_query = lambda cmd, default="n/a": (
            default if reply["v"] is None else reply["v"])
        self._check(drv, lambda raw: reply.update(v=raw))

    def test_keithley_2281s(self):
        drv = _bare(Keithley2281S)
        reply = {}
        drv._safe_query = lambda cmd, default="n/a": (
            default if reply["v"] is None else reply["v"])
        with patch("time.sleep"):
            self._check(drv, lambda raw: reply.update(v=raw))

    def test_dp800(self):
        drv = _bare(RigolDP800)
        reply = {}

        def query(cmd):
            if reply["v"] is None:
                raise RuntimeError("VISA timeout")
            return reply["v"]
        drv.instr = types.SimpleNamespace(query=query)
        with patch("time.sleep"):
            self._check(drv, lambda raw: reply.update(v=raw))

    def test_dp800_retries_past_one_unclear_reply(self):
        drv = _bare(RigolDP800)
        answers = iter(["", "ON"])
        drv.instr = types.SimpleNamespace(query=lambda cmd: next(answers))
        with patch("time.sleep"):
            self.assertIs(drv.output_state(), True)

    def test_keithley_monitor_state_unknown_output(self):
        # The 2281S monitor reads :OUTP? itself, without mode switching.
        drv = _bare(Keithley2281S)
        drv._safe_query_no_mode = lambda cmd, default="n/a": {
            ":OUTP?": "",
            ":SOUR1:VOLT?": "5.0",
            ":SOUR1:CURR?": "1.0",
        }.get(cmd, default)
        drv._determine_operating_mode_no_mode = lambda: "CV"
        state = drv.get_monitor_state()
        self.assertIsNone(state["enabled"])
        self.assertIsNone(state["voltage"])


class SupplyNetDefaultTests(unittest.TestCase):
    """The base ``output_state`` covers drivers whose ``output_is_enabled``
    raises on failure, and ``get_monitor_state`` reports through it."""

    def _supply(self, output_is_enabled):
        class Fake(supply_net_mod.SupplyNet):
            def voltage(self, value=None, ocp=None, ovp=None): pass
            def current(self, value=None, ocp=None, ovp=None): pass
            def enable(self): pass
            def disable(self): pass
            def set_mode(self): pass
            def state(self): pass
            def clear_ocp(self): pass
            def clear_ovp(self): pass
            def ocp(self, value=None): pass
            def ovp(self, value=None): pass
        Fake.output_is_enabled = output_is_enabled
        return Fake()

    def test_raising_read_is_unknown(self):
        def boom(self, channel=None):
            raise RuntimeError("query failed")
        sup = self._supply(boom)
        self.assertIsNone(sup.output_state())
        self.assertIsNone(sup.get_monitor_state()["enabled"])

    def test_answers_pass_through(self):
        for value in (True, False):
            with self.subTest(value=value):
                sup = self._supply(lambda self, channel=None, v=value: v)
                self.assertIs(sup.output_state(), value)
                self.assertIs(sup.get_monitor_state()["enabled"], value)


class BatteryStateTests(unittest.TestCase):
    def _battery(self, replies):
        drv = _bare(KeithleyBattery)
        drv._safe_query = lambda cmd, default="": replies.get(cmd, default)
        drv._mode_string = lambda: "Static"
        drv.current_model = lambda: "discharge"
        return drv

    def test_output_state(self):
        for raw, expected in REPLIES:
            with self.subTest(raw=raw):
                replies = {} if raw is None else {":BATT:OUTP?": raw}
                drv = self._battery(replies)
                self.assertIs(drv._batt_output_state(), expected)
                self.assertIs(drv._is_batt_output_on(), expected is True)

    def test_failed_measurements_are_none_not_zero(self):
        # Nothing answers: no output state and no readings -- not "off, 0 V".
        state = self._battery({}).get_monitor_state()
        for key in ("enabled", "terminal_voltage", "current", "soc", "voc"):
            with self.subTest(key=key):
                self.assertIsNone(state[key])

    def test_real_readings_still_parse(self):
        state = self._battery({
            ":BATT:OUTP?": "1",
            ":BATT:SIM:TVOL?": "3.7 V",
            ":BATT:SIM:CURR?": "+1.5E-01A,+3.7E+00V",
            ":BATT:SIM:SOC?": "0",
        }).get_monitor_state()
        self.assertIs(state["enabled"], True)
        self.assertAlmostEqual(state["terminal_voltage"], 3.7)
        self.assertAlmostEqual(state["current"], 0.15)
        self.assertEqual(state["soc"], 0.0)   # a real 0 stays 0


class ELoadStateTests(unittest.TestCase):
    def test_input_state(self):
        for raw, expected in REPLIES:
            if raw is None:
                continue  # a failed query raises out of _query, as before
            with self.subTest(raw=raw):
                drv = _bare(RigolDL3021)
                drv._query = lambda cmd, r=raw: r.strip()
                self.assertIs(drv.input_state(), expected)
                self.assertIs(drv.get_input_state(), expected is True)


class YkushStateTests(unittest.TestCase):
    def setUp(self):
        self._prior = (ykush_mod._PORT_UP, ykush_mod._PORT_DOWN)
        ykush_mod._PORT_UP, ykush_mod._PORT_DOWN = 1, 0
        self.addCleanup(self._restore)

    def _restore(self):
        ykush_mod._PORT_UP, ykush_mod._PORT_DOWN = self._prior

    def _read(self, dev):
        return ykush_mod.YKUSHUSBNet._read_enabled(dev, 1)

    def test_up_and_down(self):
        self.assertIs(self._read(types.SimpleNamespace(get_port_state=lambda p: 1)), True)
        self.assertIs(self._read(types.SimpleNamespace(get_port_state=lambda p: 0)), False)

    def test_pykush_error_value_is_not_enabled(self):
        # pykush returns YKUSH_PORT_STATE_ERROR (255) when the hub did not
        # answer; bool(255) used to report the port as enabled.
        with self.assertRaises(PortStateError):
            self._read(types.SimpleNamespace(get_port_state=lambda p: 255))

    def test_older_read_method_is_used(self):
        dev = types.SimpleNamespace(switch_port_state_get=lambda p: 1)
        self.assertIs(self._read(dev), True)

    def test_no_read_method_is_not_disabled(self):
        with self.assertRaises(PortStateError):
            self._read(types.SimpleNamespace())


class SharedHubPortTests(unittest.TestCase):
    def test_two_nets_on_one_port_both_get_its_state(self):
        nets = {
            "dut_power": {"port": 3, "instrument": "fake", "address": "hub"},
            "dut_power_alias": {"port": 3, "instrument": "fake", "address": "hub"},
            "other": {"port": 4, "instrument": "fake", "address": "hub"},
        }

        class Controller:
            asked = []

            def _lock_key(self):
                return "hub"

            def states(self, ports, timeout=None):
                Controller.asked.append(sorted(ports))
                return {3: True, 4: False}

        with patch.object(usb_dispatcher, "_load_net_definitions", return_value=nets), \
             patch.object(usb_dispatcher, "_controller_for", return_value=Controller()):
            out = usb_dispatcher.states()
        self.assertEqual(out, {"dut_power": True, "dut_power_alias": True, "other": False})
        self.assertEqual(Controller.asked, [[3, 4]])


class LabJackPinZeroTests(unittest.TestCase):
    def test_batch_pin(self):
        self.assertEqual(hardware_service._batch_pin({"pin": 0}), 0)
        self.assertEqual(hardware_service._batch_pin({"pin": "FIO3"}), "FIO3")
        self.assertEqual(hardware_service._batch_pin({}), "")

    def test_integer_zero_is_bit_zero(self):
        self.assertEqual(
            hardware_service._dio_bit_position(hardware_service._batch_pin({"pin": 0})), 0)

    def test_record_pin(self):
        cases = [
            ({"pin": 0}, 0), ({"pin": 0, "channel": 5}, 0), ({"channel": 0}, 0),
            ({"pin": "", "channel": "AIN2"}, "AIN2"), ({}, ""),
        ]
        for rec, expected in cases:
            with self.subTest(rec=rec):
                self.assertEqual(nets_handler._record_pin(rec), expected)


class NetsStateStructuredTests(unittest.TestCase):
    def test_on_off_slot(self):
        self.assertEqual(nets_handler._on_off(True), "on")
        self.assertEqual(nets_handler._on_off(False), "off")
        self.assertEqual(nets_handler._on_off(None), "?")

    def test_split_brief(self):
        split = nets_handler._split_brief
        self.assertEqual(split("power-supply", ("CH1/on/3.30V/0.100A", True)),
                         ("CH1/on/3.30V/0.100A", True))
        self.assertEqual(split("power-supply", ("CH1/?/?V/?A", None)),
                         ("CH1/?/?V/?A", None))
        self.assertEqual(split("usb", "enabled"), ("enabled", True))
        self.assertEqual(split("usb", "disabled"), ("disabled", False))
        self.assertEqual(split("usb", None), (None, None))
        self.assertEqual(split("gpio", "HIGH (1)"), ("HIGH (1)", None))
        # A non-bool never becomes one.
        self.assertEqual(split("eload", ("CC/?/1.00V/0.100A", "ON")),
                         ("CC/?/1.00V/0.100A", None))

    def test_entry_carries_enabled_only_when_known(self):
        entry = nets_handler._entry
        self.assertEqual(entry("p", "power-supply", "CH1/on/1V/1A", enabled=True)["enabled"], True)
        self.assertEqual(entry("p", "power-supply", "CH1/off/1V/1A", enabled=False)["enabled"], False)
        self.assertNotIn("enabled", entry("p", "power-supply", "CH1/?/1V/1A", enabled=None))
        self.assertNotIn("enabled", entry("g", "gpio", "HIGH (1)"))
        null = entry("p", "power-supply", None, "deadline", enabled=True)
        self.assertNotIn("enabled", null)
        self.assertEqual(null["reason"], "deadline")

    def test_per_net_probe_reports_enabled(self):
        probes = {"power-supply": lambda name: ("CH1/on/3.30V/0.100A", True),
                  "battery": lambda name: ("CH1/?/?V/?A/?%", None)}
        with patch.dict(nets_handler._BRIEF_PROBES, probes):
            on = nets_handler._probe_net_state({"name": "vdd", "role": "power-supply"})
            unknown = nets_handler._probe_net_state({"name": "batt", "role": "battery"})
        self.assertEqual(on, {"name": "vdd", "role": "power-supply",
                              "state": "CH1/on/3.30V/0.100A", "enabled": True})
        self.assertEqual(unknown, {"name": "batt", "role": "battery",
                                   "state": "CH1/?/?V/?A/?%"})

    def test_usb_batch_reports_enabled(self):
        recs = [{"name": n, "role": "usb", "instrument": "Acroname_8Port", "address": "a"}
                for n in ("usb1", "usb2", "usb3")]

        def batch(names, causes=None, codes=None, deadline=None):
            return {"usb1": "enabled", "usb2": "disabled", "usb3": None}
        with patch.dict(nets_handler._BATCH_PROBES, {"usb": batch}):
            out = {e["name"]: e for e in nets_handler._probe_group(recs)}
        self.assertIs(out["usb1"]["enabled"], True)
        self.assertIs(out["usb2"]["enabled"], False)
        self.assertNotIn("enabled", out["usb3"])
        self.assertIsNone(out["usb3"]["state"])

    def test_supply_brief_unknown_output_is_not_off(self):
        device = MagicMock()
        device.get_monitor_state.return_value = {
            "enabled": None, "voltage": 3.3, "current": None}
        with patch("lager.dispatchers.helpers.resolve_net_proxy",
                   return_value=("supply1", {}, 1)), \
             patch("lager.nets.device.Device", return_value=device):
            text, enabled = nets_handler._brief_supply("vdd")
        self.assertEqual(text, "CH1/?/3.30V/?A")
        self.assertIsNone(enabled)

    def test_supply_brief_known_output(self):
        device = MagicMock()
        device.get_monitor_state.return_value = {
            "enabled": False, "voltage": 0.0, "current": 0.0}
        with patch("lager.dispatchers.helpers.resolve_net_proxy",
                   return_value=("supply1", {}, 2)), \
             patch("lager.nets.device.Device", return_value=device):
            self.assertEqual(nets_handler._brief_supply("vdd"),
                             ("CH2/off/0.00V/0.000A", False))

    def test_watt_brief_missing_fields_are_not_zero(self):
        dev = MagicMock()
        dev.measure.return_value = {"voltage": 5.0}
        with patch("lager.http_handlers.net_command._proxy", return_value=dev):
            self.assertEqual(nets_handler._brief_watt("pwr"), "?A/5.000V/?W")

    def test_energy_analyzer_brief_missing_fields_are_not_zero(self):
        dev = MagicMock()
        dev.measure.return_value = {"current": {"mean": 0.25}}
        with patch("lager.http_handlers.net_command._proxy", return_value=dev):
            self.assertEqual(nets_handler._brief_energy_analyzer("ea"), "0.2500A/?V/?W")


if __name__ == "__main__":
    unittest.main()
