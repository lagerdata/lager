# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for Yepkit YKUSH3 support.

The YKUSH3 (04d8:f11b) has three switchable USB ports, which are ``usb`` nets
on the existing YKUSH driver, and a switchable 5V output, which is not a USB
port and is a ``power-supply`` net on box/lager/power/supply/ykush3.py. These
tests cover detection, routing on every supply path, and the 5V driver
against a stubbed pykush, so they need no hardware.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'brainstem', 'hid',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'flask_socketio',
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

from lager.automation.usb_hub import ykush as ykush_mod  # noqa: E402
from lager.automation.usb_hub import dispatcher as usb_dispatcher  # noqa: E402
from lager.dispatchers import helpers  # noqa: E402
from lager.exceptions import SupplyBackendError  # noqa: E402
from lager.http_handlers import usb_scanner  # noqa: E402
from lager.nets import net as net_mod  # noqa: E402
from lager.nets.mappers.rigol_dp800 import RigolDP800FunctionMapper  # noqa: E402
from lager.power.supply import ykush3  # noqa: E402
from lager.power.supply.dispatcher import SupplyDispatcher  # noqa: E402

ADDRESS = "USB0::0x04D8::0xF11B::YK3TEST01::INSTR"


class _FakeHub:
    """Stub pykush.YKUSH recording the 5V output commands."""

    instances = []
    output_state = 0
    accept = True

    def __init__(self, serial=None, path=None):
        self.serial = serial
        self.calls = []
        _FakeHub.instances.append(self)

    def set_power_output_on(self):
        self.calls.append("on")
        if _FakeHub.accept:
            _FakeHub.output_state = 1
        return _FakeHub.accept

    def set_power_output_off(self):
        self.calls.append("off")
        if _FakeHub.accept:
            _FakeHub.output_state = 0
        return _FakeHub.accept

    def get_power_output_state(self):
        return _FakeHub.output_state


class _HubTestCase(unittest.TestCase):
    def setUp(self):
        self._prior = (ykush_mod._YKUSH_CLS, ykush_mod._PORT_UP,
                       ykush_mod._PORT_DOWN, ykush_mod._LIBRARY_CHECKED,
                       ykush_mod.YKUSHUSBNet._path_cache,
                       ykush_mod.YKUSHUSBNet._path_open_supported)
        ykush_mod._YKUSH_CLS = _FakeHub
        ykush_mod._PORT_UP, ykush_mod._PORT_DOWN = 1, 0
        ykush_mod._LIBRARY_CHECKED = True
        ykush_mod.YKUSHUSBNet._path_cache = {}
        ykush_mod.YKUSHUSBNet._path_open_supported = True
        _FakeHub.instances = []
        _FakeHub.output_state = 0
        _FakeHub.accept = True

    def tearDown(self):
        (ykush_mod._YKUSH_CLS, ykush_mod._PORT_UP, ykush_mod._PORT_DOWN,
         ykush_mod._LIBRARY_CHECKED, ykush_mod.YKUSHUSBNet._path_cache,
         ykush_mod.YKUSHUSBNet._path_open_supported) = self._prior


class DetectionTests(unittest.TestCase):
    def test_scanner_catalogs_the_ykush3(self):
        meta = usb_scanner.SUPPORTED_USB["YKUSH3_Hub"]
        self.assertEqual((meta["vid"], meta["pid"]), ("04d8", "f11b"))
        self.assertEqual(meta["net_type"], ["usb", "power-supply", "gpio"])

    def test_channels_are_three_ports_one_output_and_three_pins(self):
        self.assertEqual(usb_scanner.CHANNEL_MAPS["YKUSH3_Hub"],
                         {"usb": ["1", "2", "3"], "power-supply": ["1"],
                          "gpio": ["1", "2", "3"]})

    def test_original_ykush_offers_no_power_output(self):
        self.assertEqual(usb_scanner.SUPPORTED_USB["YKUSH_Hub"]["net_type"], ["usb"])


class RoutingTests(unittest.TestCase):
    def test_usb_nets_use_the_ykush_driver(self):
        ctl = usb_dispatcher._controller_for(
            {"instrument": "YKUSH3_Hub", "address": ADDRESS})
        self.assertIsInstance(ctl, ykush_mod.YKUSHUSBNet)

    def test_supply_dispatcher_picks_the_5v_driver(self):
        self.assertIs(SupplyDispatcher()._choose_driver("YKUSH3_Hub"),
                      ykush3.YKUSH3PowerOutput)

    def test_hardware_service_module_is_ykush3(self):
        self.assertEqual(helpers._supply_module_for_instrument("YKUSH3_Hub"), "ykush3")
        # The original YKUSH has no 5V output, so it is never a supply.
        self.assertIsNone(helpers._supply_module_for_instrument("YKUSH_Hub"))

    def test_python_api_mapper(self):
        net = MagicMock(channel=1)
        mapper = net_mod.mapper_factory(net, "ykush3", {"address": ADDRESS})
        self.assertIsInstance(mapper, RigolDP800FunctionMapper)
        self.assertEqual(mapper.device.device_name, "ykush3")

    def test_python_api_mapper_from_visa_address(self):
        net = MagicMock(channel=1)
        mapper = net_mod.mapper_factory(net, ADDRESS, {"instrument": "YKUSH3_Hub"})
        self.assertEqual(mapper.device.device_name, "ykush3")


class HubReenumerationTests(unittest.TestCase):
    """After the hub re-enumerates (reset, replug, power blip), a process
    that already used it gets pykush's "YKUSH device not found" forever.
    box_http_server restarted itself on that; hardware_service, which serves
    the 5V net, did not, so the net stayed broken until a manual restart."""

    def test_ykush_not_found_is_an_open_failure(self):
        from lager.util import self_restart
        self.assertTrue(self_restart.looks_like_open_failure(
            Exception("YKUSH device not found")))

    def test_hardware_service_restarts_on_the_hubs_address(self):
        from unittest.mock import patch
        from lager import hardware_service as hw
        from lager.util import self_restart

        class _Stale:
            def output_state(self, channel=None):
                raise Exception("YKUSH device not found")

        restart = MagicMock()
        restart.looks_like_open_failure = self_restart.looks_like_open_failure
        net_info = {"name": "ykush3_5v", "address": ADDRESS, "channel": 1,
                    "instrument": "YKUSH3_Hub"}
        hw.device_cache.clear()
        hw.device_cache[("ykush3", ADDRESS)] = _Stale()
        try:
            with patch.object(hw, "_self_restart", restart):
                resp = hw.app.test_client().post("/invoke", json={
                    "device": "ykush3", "function": "output_state",
                    "args": [], "kwargs": {"channel": 1}, "net_info": net_info})
        finally:
            hw.device_cache.clear()
        self.assertEqual(resp.status_code, 503)
        restart.self_restart_after_reply.assert_called_once()
        self.assertEqual(restart.self_restart_after_reply.call_args.args[0], ADDRESS)


class PowerOutputTests(_HubTestCase):
    def setUp(self):
        super().setUp()
        self.drv = ykush3.create_device(
            {"address": ADDRESS, "instrument": "YKUSH3_Hub", "pin": "1"})

    def test_shares_the_usb_nets_hub_lock(self):
        usb_net = ykush_mod.YKUSHUSBNet({"address": ADDRESS})
        self.assertEqual(self.drv._hub._lock_key(), usb_net._lock_key())
        self.assertEqual(self.drv._hub.serial, "YK3TEST01")

    def test_enable_and_disable_switch_the_output(self):
        self.drv.enable()
        self.assertTrue(self.drv.output_is_enabled())
        self.drv.disable()
        self.assertFalse(self.drv.output_is_enabled())
        calls = [c for hub in _FakeHub.instances for c in hub.calls]
        self.assertEqual(calls, ["on", "off"])

    def test_rejected_command_raises(self):
        _FakeHub.accept = False
        with self.assertRaises(SupplyBackendError):
            self.drv.enable_output(1)

    def test_a_hub_that_will_not_open_raises_from_the_state_read(self):
        """Bench, JUL-33: after a hub reset `supply state` read UNKNOWN
        forever, because the failure never reached hardware_service's
        self-restart. Only "answered but unreadable" is unknown."""
        class _Gone(_FakeHub):
            def __init__(self, serial=None, path=None):
                raise Exception("YKUSH device not found")
        ykush_mod._YKUSH_CLS = _Gone
        with self.assertRaises(Exception) as ctx:
            self.drv.get_monitor_state(1)
        self.assertIn("YKUSH device not found", str(ctx.exception))
        with self.assertRaises(Exception):
            self.drv.output_state(1)

    def test_unreadable_state_is_unknown_not_off(self):
        _FakeHub.output_state = 255
        with self.assertRaises(Exception):
            self.drv.output_is_enabled()
        self.assertIsNone(self.drv.output_state())

    def test_five_volts_is_accepted_anything_else_refused(self):
        self.drv.set_channel_voltage(5.0, 1)
        self.drv.voltage(5)
        with self.assertRaises(SupplyBackendError):
            self.drv.set_channel_voltage(3.3, 1)
        self.assertEqual(self.drv.get_channel_voltage(), 5.0)

    def test_current_and_protection_are_refused(self):
        for call in (lambda: self.drv.current(0.5),
                     lambda: self.drv.set_channel_current(0.5),
                     lambda: self.drv.ocp(1.0),
                     lambda: self.drv.ovp(6.0),
                     lambda: self.drv.voltage(5, ocp=1.0),
                     lambda: self.drv.clear_ocp(),
                     lambda: self.drv.clear_ovp(),
                     lambda: self.drv.measure_voltage()):
            with self.assertRaises(SupplyBackendError):
                call()

    def test_monitor_state_reports_no_made_up_measurements(self):
        _FakeHub.output_state = 1
        state = self.drv.get_monitor_state(1)
        self.assertIs(state["enabled"], True)
        self.assertEqual(state["voltage_set"], 5.0)
        self.assertEqual(state["voltage_max"], 5.0)
        for key in ("voltage", "current", "power", "current_set"):
            self.assertIsNone(state[key], key)

    def test_reports_no_limits_for_the_handler_to_check(self):
        self.assertEqual(self.drv.get_channel_limits(1),
                         {"voltage_max": 0, "current_max": 0,
                          "ovp_max": 0, "ocp_max": 0})


class SupplyCommandTests(_HubTestCase):
    """POST /supply/command against the real driver and the real limit read.

    Bench run on a YKUSH3: a 5V voltage_max made the handler refuse
    `voltage 6` and `voltage 5 --ovp 6` with generic range errors before the
    driver ran, implying a range the output does not have. Every refusal must
    be the driver's own."""

    def setUp(self):
        super().setUp()
        from flask import Flask
        from lager.http_handlers import supply as supply_handler
        self.handler = supply_handler
        self.drv = ykush3.create_device({"address": ADDRESS, "pin": "1"})
        self._orig = (supply_handler._resolve_supply_proxy,
                      supply_handler.resolve_net_proxy)
        supply_handler._resolve_supply_proxy = lambda netname: (
            self.drv, 1, supply_handler._read_limits(self.drv, 1))

        def _no_net(netname, role, error_class):
            raise SupplyBackendError("no saved nets in test")
        supply_handler.resolve_net_proxy = _no_net
        app = Flask(__name__)
        supply_handler.register_supply_routes(app)
        self.client = app.test_client()

    def tearDown(self):
        (self.handler._resolve_supply_proxy,
         self.handler.resolve_net_proxy) = self._orig
        super().tearDown()

    def _post(self, action, **params):
        resp = self.client.post("/supply/command", json={
            "netname": "ykush3_5v", "action": action, "params": params})
        return resp.status_code, resp.get_json()

    def _refused(self, action, expected, **params):
        status, body = self._post(action, **params)
        self.assertFalse(body["success"], body)
        self.assertIn(expected, body["error"])
        self.assertNotIn("exceeds", body["error"])
        self.assertEqual(_FakeHub.output_state, 0)

    def test_ovp_above_5v_is_not_supported(self):
        self._refused("voltage", "Over-voltage protection is not supported",
                      value=5, ovp=6)

    def test_ocp_is_not_supported(self):
        self._refused("voltage", "Over-current protection is not supported",
                      value=5, ocp=9)

    def test_standalone_ovp_and_ocp_are_not_supported(self):
        self._refused("ovp", "Over-voltage protection is not supported", value=6)
        self._refused("ocp", "Over-current protection is not supported", value=9)

    def test_voltage_above_5v_is_fixed_at_5v(self):
        self._refused("voltage", "fixed at 5V", value=6)

    def test_voltage_below_5v_is_fixed_at_5v(self):
        self._refused("voltage", "fixed at 5V", value=3.3)

    def test_five_volts_is_accepted(self):
        status, body = self._post("voltage", value=5)
        self.assertEqual(status, 200)
        self.assertTrue(body["success"], body)

    def test_state_message(self):
        status, body = self._post("state")
        self.assertEqual(status, 200)
        self.assertIn("Channel 1: OFF, Set: 5.0V/n/a, Measured: n/a/n/a",
                      body["message"])


class _GpioHub(_FakeHub):
    """Answers the hub's GPIO commands the way a YKUSH3 did on the bench:
    01 <cmd> <pin> <level>, then 4 bytes of leftovers."""
    sent = []
    levels = {1: 0, 2: 0, 3: 0}
    reply_override = None

    def _raw_sendreceive(self, packet):
        _GpioHub.sent.append(list(packet))
        if _GpioHub.reply_override is not None:
            return list(_GpioHub.reply_override)
        cmd, pin = packet[0], packet[1]
        if cmd == 0x31:
            _GpioHub.levels[pin] = packet[2]
        return [0x01, cmd, pin, _GpioHub.levels.get(pin, 0), 0x79, 0xc0, 0x00, 0x09]


class GpioTests(_HubTestCase):
    def setUp(self):
        super().setUp()
        from lager.io.gpio import ykush3_gpio
        self.mod = ykush3_gpio
        ykush_mod._YKUSH_CLS = _GpioHub
        _GpioHub.sent = []
        _GpioHub.levels = {1: 0, 2: 0, 3: 0}
        _GpioHub.reply_override = None
        self.pin = ykush3_gpio.YKUSH3GPIO("ykush3_gpio2", "2", address=ADDRESS)

    def test_write_and_read_send_the_hubs_commands(self):
        self.pin.output("high")
        self.assertEqual(self.pin.input(), 1)
        self.pin.output(0)
        self.assertEqual(self.pin.input(), 0)
        self.assertEqual(_GpioHub.sent, [[0x31, 2, 1], [0x30, 2],
                                         [0x31, 2, 0], [0x30, 2]])

    def test_never_sends_the_port_control_enable(self):
        """0x32 hands the USB ports to the GPIO pins (bench, JUL-33)."""
        self.pin.output(1)
        self.pin.input()
        self.pin.wait_for_level(1, timeout=1)
        self.assertNotIn(0x32, [p[0] for p in _GpioHub.sent])

    def test_shares_the_hubs_lock(self):
        usb_net = ykush_mod.YKUSHUSBNet({"address": ADDRESS})
        self.assertEqual(self.pin._hub._lock_key(), usb_net._lock_key())

    def test_an_unacknowledged_command_raises(self):
        _GpioHub.reply_override = [0xff] * 8   # pykush's "no answer"
        with self.assertRaises(self.mod.GPIOBackendError):
            self.pin.output(1)
        with self.assertRaises(self.mod.GPIOBackendError):
            self.pin.input()

    def test_a_reply_for_another_pin_raises(self):
        _GpioHub.reply_override = [0x01, 0x30, 0x03, 0x01, 0, 0, 0, 0]
        with self.assertRaises(self.mod.GPIOBackendError):
            self.pin.input()

    def test_bad_pins_and_levels_are_refused_before_sending(self):
        for bad in (0, 4, "FIO0", None):
            with self.assertRaises(self.mod.GPIOBackendError):
                self.mod.YKUSH3GPIO("x", bad, address=ADDRESS)
        with self.assertRaises(self.mod.GPIOBackendError):
            self.pin.output("maybe")
        self.assertEqual(_GpioHub.sent, [])


class GpioRoutingTests(unittest.TestCase):
    def test_gpio_dispatcher_picks_the_ykush3_driver(self):
        from lager.io.gpio import dispatcher
        from lager.io.gpio.ykush3_gpio import YKUSH3GPIO
        disp = dispatcher.GPIODispatcher()
        self.assertIs(disp._choose_driver("YKUSH3_Hub"), YKUSH3GPIO)
        drv = disp._make_driver({"instrument": "YKUSH3_Hub", "address": ADDRESS},
                                "ykush3_gpio1_routing", 1)
        self.assertIsInstance(drv, YKUSH3GPIO)
        self.assertEqual(drv.address, ADDRESS)

    def test_python_api_returns_the_ykush3_driver(self):
        from unittest.mock import patch
        from lager.io.gpio.ykush3_gpio import YKUSH3GPIO
        from lager.nets.constants import NetType
        rec = {"name": "ykush3_gpio3", "role": "gpio", "instrument": "YKUSH3_Hub",
               "address": ADDRESS, "pin": "3"}
        with patch.object(net_mod.Net, "get_local_nets", classmethod(lambda cls: [rec])):
            drv = net_mod.Net.get_from_saved_json("ykush3_gpio3", NetType.GPIO)
        self.assertIsInstance(drv, YKUSH3GPIO)
        self.assertEqual((drv.pin, drv.address), (3, ADDRESS))

    def test_device_id_is_the_hub(self):
        from lager.nets.device_identity import physical_device_id
        self.assertEqual(
            physical_device_id("gpio", "YKUSH3_Hub", {"address": ADDRESS}),
            "ykush:" + ADDRESS)

    def test_hardware_service_restarts_on_the_hub_for_a_gpio_net(self):
        from lager import hardware_service as hw
        self.assertEqual(hw._restart_address(None, "ykush:" + ADDRESS), ADDRESS)
        self.assertIsNone(hw._restart_address(None, "ykush:ANY"))


if __name__ == "__main__":
    unittest.main()
