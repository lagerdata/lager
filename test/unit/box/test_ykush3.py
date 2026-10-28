# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for Yepkit YKUSH3 support.

The YKUSH3 (04d8:f11b) has three switchable USB ports, which are ``usb`` nets
on the existing YKUSH driver, and three GPIO pins, which are ``gpio`` nets on
box/lager/io/gpio/ykush3_gpio.py. Its switchable 5V output is deliberately
not a net. These tests cover detection, routing and the GPIO driver against a
stubbed pykush, so they need no hardware.
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
from lager.http_handlers import usb_scanner  # noqa: E402
from lager.nets import net as net_mod  # noqa: E402

ADDRESS = "USB0::0x04D8::0xF11B::YK3TEST01::INSTR"


class _FakeHub:
    """Stub pykush.YKUSH; subclasses answer the commands a test needs."""

    def __init__(self, serial=None, path=None):
        self.serial = serial


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

    def tearDown(self):
        (ykush_mod._YKUSH_CLS, ykush_mod._PORT_UP, ykush_mod._PORT_DOWN,
         ykush_mod._LIBRARY_CHECKED, ykush_mod.YKUSHUSBNet._path_cache,
         ykush_mod.YKUSHUSBNet._path_open_supported) = self._prior


class DetectionTests(unittest.TestCase):
    def test_scanner_catalogs_the_ykush3(self):
        meta = usb_scanner.SUPPORTED_USB["YKUSH3_Hub"]
        self.assertEqual((meta["vid"], meta["pid"]), ("04d8", "f11b"))
        self.assertEqual(meta["net_type"], ["usb", "gpio"])

    def test_channels_are_three_ports_and_three_pins(self):
        self.assertEqual(usb_scanner.CHANNEL_MAPS["YKUSH3_Hub"],
                         {"usb": ["1", "2", "3"], "gpio": ["1", "2", "3"]})

    def test_original_ykush_is_usb_only(self):
        self.assertEqual(usb_scanner.SUPPORTED_USB["YKUSH_Hub"]["net_type"], ["usb"])


class RoutingTests(unittest.TestCase):
    def test_usb_nets_use_the_ykush_driver(self):
        ctl = usb_dispatcher._controller_for(
            {"instrument": "YKUSH3_Hub", "address": ADDRESS})
        self.assertIsInstance(ctl, ykush_mod.YKUSHUSBNet)

    def test_the_5v_output_is_not_a_power_supply(self):
        """Left out on purpose: it only switches on and off, so most of a
        power-supply net's controls would not apply."""
        self.assertIsNone(helpers._supply_module_for_instrument("YKUSH3_Hub"))


class HubReenumerationTests(unittest.TestCase):
    """After the hub re-enumerates (reset, replug, power blip), a process
    that already used it gets pykush's "YKUSH device not found" forever.
    box_http_server restarted itself on that; hardware_service, which serves
    the GPIO nets, did not, so a net stayed broken until a manual restart."""

    def test_ykush_not_found_is_an_open_failure(self):
        from lager.util import self_restart
        self.assertTrue(self_restart.looks_like_open_failure(
            Exception("YKUSH device not found")))

    def test_hardware_service_restarts_on_the_hubs_address(self):
        from unittest.mock import patch
        from lager import hardware_service as hw
        from lager.util import self_restart

        class _Stale:
            def input(self):
                raise Exception("YKUSH device not found")

        restart = MagicMock()
        restart.looks_like_open_failure = self_restart.looks_like_open_failure
        net_info = {"name": "gpio1", "instrument": "YKUSH3_Hub",
                    "device_id": "ykush:" + ADDRESS}
        hw.device_cache.clear()
        hw.device_cache[("gpio_hs", hw.get_net_info_hash(net_info))] = _Stale()
        try:
            with patch.object(hw, "_self_restart", restart):
                resp = hw.app.test_client().post("/invoke", json={
                    "device": "gpio_hs", "function": "input",
                    "args": [], "kwargs": {}, "net_info": net_info})
        finally:
            hw.device_cache.clear()
        self.assertEqual(resp.status_code, 503)
        restart.self_restart_after_reply.assert_called_once()
        self.assertEqual(restart.self_restart_after_reply.call_args.args[0], ADDRESS)


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
        """0x32 hands the USB ports to the GPIO pins (confirmed on hardware)."""
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
