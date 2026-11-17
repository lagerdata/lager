# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""GPIO, ADC and DAC nets in a script, for a container whose USB is on the host.

Covers the stand-ins a script gets in place of the drivers (each call sent to
the hardware service's adapter for that role, with the net) and the switch in
``lager/nets/net.py`` staying off unless ``LAGER_HOSTLINK`` is ``1``.
No hardware service or instrument is involved.
"""
import os
import unittest
from unittest.mock import patch

from lager.hostlink import io_proxy
from lager.io.adc.adc_net import ADCBase
from lager.io.dac.dac_net import DACBase
from lager.io.gpio.gpio_net import GPIOBase

NETS = [
    {"name": "ljio", "role": "gpio", "instrument": "LabJack_U3", "pin": "FIO4",
     "address": "USB0::0x0CD5::0x0003::port-1-1.3::INSTR"},
    {"name": "ljadc", "role": "adc", "instrument": "LabJack_U3", "pin": "AIN0",
     "address": "USB0::0x0CD5::0x0003::port-1-1.3::INSTR"},
    {"name": "ljdac", "role": "dac", "instrument": "LabJack_U3", "pin": "DAC0",
     "address": "USB0::0x0CD5::0x0003::port-1-1.3::INSTR"},
]


class _Devices:
    """Records what would be sent to the hardware service."""

    def __init__(self, testcase):
        self.calls = []
        self.reply = 1
        outer = self

        class Device:
            def __init__(self, device_name, net_info=None, timeout=None):
                self.device_name, self.net_info, self.timeout = device_name, net_info, timeout

            def __getattr__(self, function):
                def call(*args, **kwargs):
                    outer.calls.append((self.device_name, self.net_info["name"], function,
                                        args, kwargs, self.timeout))
                    return outer.reply
                return call

        patcher = patch.object(io_proxy, "Device", Device)
        patcher.start()
        testcase.addCleanup(patcher.stop)


class StandInTests(unittest.TestCase):
    def setUp(self):
        self.devices = _Devices(self)

    def test_gpio_reads_and_writes_through_its_adapter(self):
        gpio = io_proxy.for_role("gpio", "ljio", NETS[0])
        self.assertIsInstance(gpio, GPIOBase)
        self.assertEqual((gpio.name, gpio.pin), ("ljio", "FIO4"))
        self.assertEqual(gpio.input(), 1)
        self.assertIsNone(gpio.output("high"))
        self.assertEqual([c[:5] for c in self.devices.calls], [
            ("gpio_hs", "ljio", "input", (), {}),
            ("gpio_hs", "ljio", "output", ("high",), {})])

    def test_gpio_wait_outlasts_its_own_timeout(self):
        self.devices.reply = 0.25
        gpio = io_proxy.for_role("gpio", "ljio", NETS[0])
        self.assertEqual(gpio.wait_for_level(1, timeout=30, scan_rate=1000), 0.25)
        device, _, function, args, kwargs, http_timeout = self.devices.calls[-1]
        self.assertEqual((device, function, args), ("gpio_hs", "wait_for_level", (1,)))
        self.assertEqual(kwargs, {"timeout": 30, "poll_interval": 0.01, "scan_rate": 1000})
        self.assertGreater(http_timeout, 30)
        gpio.wait_for_level(0)
        self.assertGreaterEqual(self.devices.calls[-1][5], 3600)

    def test_adc_reads_a_float(self):
        self.devices.reply = "1.397"
        adc = io_proxy.for_role("adc", "ljadc", NETS[1])
        self.assertIsInstance(adc, ADCBase)
        self.assertEqual(adc.input(), 1.397)
        self.assertEqual(self.devices.calls[0][:3], ("adc_hs", "ljadc", "input"))

    def test_dac_sets_and_reads_back(self):
        self.devices.reply = 2.0
        dac = io_proxy.for_role("dac", "ljdac", NETS[2])
        self.assertIsInstance(dac, DACBase)
        self.assertIsNone(dac.output(2.0))
        self.assertEqual((dac.get_voltage(), dac.input()), (2.0, 2.0))
        self.assertEqual([c[2:4] for c in self.devices.calls],
                         [("output", (2.0,)), ("input", ()), ("input", ())])

    def test_pin_falls_back_to_channel_and_other_roles_have_no_stand_in(self):
        gpio = io_proxy.for_role("gpio", "g", {"name": "g", "role": "gpio", "channel": "3"})
        self.assertEqual(gpio.pin, "3")
        self.assertIsNone(io_proxy.for_role("uart", "u", {"name": "u"}))

    def test_adapters_have_the_methods_the_stand_ins_call(self):
        from lager import adc_hs, dac_hs, gpio_hs
        for adapter, methods in ((gpio_hs.GPIOHardwareAdapter, ("input", "output", "wait_for_level")),
                                 (adc_hs.ADCHardwareAdapter, ("input",)),
                                 (dac_hs.DACHardwareAdapter, ("input", "output"))):
            for method in methods:
                self.assertTrue(callable(getattr(adapter, method)), (adapter, method))


class NetFactorySwitchTests(unittest.TestCase):
    """The one Linux file this touches: default off."""

    def setUp(self):
        from lager.nets import net as net_module
        self.net_module = net_module
        patcher = patch.object(net_module.Net, "get_local_nets", classmethod(lambda cls: list(NETS)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self, value, name, role):
        env = {k: v for k, v in os.environ.items() if k != "LAGER_HOSTLINK"}
        if value is not None:
            env["LAGER_HOSTLINK"] = value
        with patch.dict(os.environ, env, clear=True):
            return self.net_module.Net.get(name, getattr(self.net_module.NetType, role))

    def test_unset_or_any_other_value_selects_the_box_driver(self):
        for value in (None, "0", "", "true"):
            self.assertEqual(type(self._get(value, "ljio", "GPIO")).__name__, "LabJackUDGPIO", value)
            self.assertEqual(type(self._get(value, "ljadc", "ADC")).__name__, "LabJackUDADC", value)
            self.assertEqual(type(self._get(value, "ljdac", "DAC")).__name__, "LabJackUDDAC", value)

    def test_one_selects_the_stand_ins(self):
        self.assertIsInstance(self._get("1", "ljio", "GPIO"), io_proxy.HostGPIO)
        self.assertIsInstance(self._get("1", "ljadc", "ADC"), io_proxy.HostADC)
        self.assertIsInstance(self._get("1", "ljdac", "DAC"), io_proxy.HostDAC)


if __name__ == "__main__":
    unittest.main()
