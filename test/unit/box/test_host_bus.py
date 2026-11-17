# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""SPI and I2C nets in a script, for a container whose USB is on the host.

Covers the stand-in drivers the dispatchers hand out (each call sent to the
hardware service's adapter for the role, results in the drivers' types,
failures as the dispatcher's own error class), the Box's unedited ``SPINet``
and ``I2CNet`` working on top of them, and the switch in ``lager/nets/net.py``
staying off unless ``LAGER_HOSTLINK`` is ``1``.
No hardware service or instrument is involved.
"""
import contextlib
import io
import os
import unittest
from unittest.mock import patch

from lager.exceptions import I2CBackendError, SPIBackendError
from lager.hostlink import bus_proxy
from lager.nets.device import DeviceError
from lager.protocols.i2c import dispatcher as i2c_dispatcher
from lager.protocols.i2c.i2c_net import I2CNet
from lager.protocols.spi import dispatcher as spi_dispatcher
from lager.protocols.spi.spi_net import SPINet

ADDRESS = "USB0::0x0CD5::0x0003::port-1-1.3::INSTR"
NETS = [
    {"name": "ljspi", "role": "spi", "instrument": "LabJack_U3", "address": ADDRESS,
     "params": {"cs_pin": 5, "clk_pin": 8, "mosi_pin": 6, "miso_pin": 7}},
    {"name": "lji2c", "role": "i2c", "instrument": "LabJack_U3", "address": ADDRESS,
     "params": {"sda_pin": 9, "scl_pin": 10}},
    {"name": "ljio", "role": "gpio", "instrument": "LabJack_U3", "pin": "FIO4",
     "address": ADDRESS},
]


class _Devices:
    """Records what would be sent to the hardware service."""

    def __init__(self, testcase):
        self.calls = []
        self.replies = {}
        self.error = None
        outer = self

        class Device:
            def __init__(self, device_name, net_info=None, timeout=None):
                self.device_name, self.net_info = device_name, net_info

            def __getattr__(self, function):
                def call(*args, **kwargs):
                    outer.calls.append((self.device_name, self.net_info["name"],
                                        function, args, kwargs))
                    if outer.error is not None:
                        raise DeviceError({"error": outer.error})
                    return outer.replies.get(function)
                return call

        patcher = patch.object(bus_proxy, "Device", Device)
        patcher.start()
        testcase.addCleanup(patcher.stop)


class _Installed(unittest.TestCase):
    """Installs the stand-ins for one test and puts the dispatchers back."""

    def setUp(self):
        self.devices = _Devices(self)
        from lager.protocols import i2c as i2c_package, spi as spi_package
        for module in (spi_dispatcher, i2c_dispatcher, spi_package, i2c_package):
            original = module._resolve_net_and_driver
            self.addCleanup(setattr, module, "_resolve_net_and_driver", original)
        self.addCleanup(setattr, bus_proxy, "_installed", False)
        self.addCleanup(bus_proxy._drivers.clear)
        for dispatcher in (spi_dispatcher, i2c_dispatcher):
            patcher = patch.object(
                dispatcher.helpers, "find_saved_net", side_effect=self._find)
            patcher.start()
            self.addCleanup(patcher.stop)
        bus_proxy.install()

    @staticmethod
    def _find(netname, error_class):
        for net in NETS:
            if net["name"] == netname:
                return dict(net)
        raise error_class(f"Net '{netname}' not found")


class SPITests(_Installed):
    def test_transfers_go_to_the_spi_adapter(self):
        self.devices.replies = {
            "read": {"words": [1, 2], "word_size": 8},
            "read_write": {"words": [0xA5, 0x3C, 0xFF, 0xFF], "word_size": 8},
        }
        spi = SPINet("ljspi", NETS[0])
        self.assertEqual(spi.transfer(n_words=4, data=[0xA5, 0x3C]),
                         [0xA5, 0x3C, 0xFF, 0xFF])
        self.assertEqual(spi.read(n_words=2, fill=0, keep_cs=True), [1, 2])
        self.assertIsNone(spi.write([6]))
        self.assertEqual(self.devices.calls, [
            ("spi_hs", "ljspi", "read_write", ([0xA5, 0x3C, 0xFF, 0xFF], False, None), {}),
            ("spi_hs", "ljspi", "read", (2, 0, True, None), {}),
            ("spi_hs", "ljspi", "read_write", ([6], False, None), {}),
        ])

    def test_config_is_applied_on_the_host_and_keeps_its_warnings(self):
        self.devices.replies = {"config": {"warnings": ["clamped to 80 kHz"]}}
        SPINet("ljspi", NETS[0]).config(mode=3, word_size=16)
        self.assertEqual(self.devices.calls, [
            ("spi_hs", "ljspi", "config", ({"mode": 3, "word_size": 16},), {})])
        driver = spi_dispatcher._resolve_net_and_driver("ljspi")
        self.assertEqual(driver.clamp_warnings, ["clamped to 80 kHz"])
        self.assertEqual(driver._word_size, 16)
        self.assertEqual(len(self.devices.calls), 1, "no overrides, no call")

    def test_one_driver_per_net_for_the_life_of_the_process(self):
        first = spi_dispatcher._resolve_net_and_driver("ljspi")
        self.assertIs(spi_dispatcher._resolve_net_and_driver("ljspi", None), first)

    def test_the_word_size_follows_the_host_driver(self):
        self.devices.replies = {"read": {"words": [0x1234], "word_size": 16}}
        spi = SPINet("ljspi", NETS[0])
        spi.read(n_words=1)
        self.assertEqual(spi_dispatcher._resolve_net_and_driver("ljspi")._word_size, 16)

    def test_the_dispatcher_action_prints_what_the_host_read(self):
        self.devices.replies = {"read": {"words": [0xDE, 0xAD], "word_size": 8}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            spi_dispatcher.read("ljspi", 2)
        self.assertEqual(out.getvalue().strip(), "de ad")

    def test_a_driver_failure_is_the_dispatchers_error_class(self):
        self.devices.error = "[SPI] Invalid SPI mode 9. Must be 0, 1, 2 or 3."
        with self.assertRaises(SPIBackendError) as caught:
            SPINet("ljspi", NETS[0]).config(mode=9)
        self.assertIn("Invalid SPI mode 9", str(caught.exception))

    def test_a_missing_net_or_wrong_role_is_refused_before_any_call(self):
        with self.assertRaises(SPIBackendError):
            spi_dispatcher._resolve_net_and_driver("nope")
        with self.assertRaises(SPIBackendError):
            spi_dispatcher._resolve_net_and_driver("lji2c")
        self.assertEqual(self.devices.calls, [])


class I2CTests(_Installed):
    def test_bus_calls_go_to_the_i2c_adapter(self):
        self.devices.replies = {"scan": [0x48], "read": [1, 2], "write_read": [3]}
        i2c = I2CNet("lji2c", NETS[1])
        self.assertEqual(i2c.scan(), [0x48])
        self.assertEqual(i2c.read(address=0x48, num_bytes=2), [1, 2])
        self.assertIsNone(i2c.write(address=0x48, data=[0x0A, 3]))
        self.assertEqual(i2c.write_read(address=0x48, data=[0x0A], num_bytes=1), [3])
        self.assertEqual(self.devices.calls, [
            ("i2c_hs", "lji2c", "scan", (0x08, 0x77, None), {}),
            ("i2c_hs", "lji2c", "read", (0x48, 2, None), {}),
            ("i2c_hs", "lji2c", "write", (0x48, [0x0A, 3], None), {}),
            ("i2c_hs", "lji2c", "write_read", (0x48, [0x0A], 1, None), {}),
        ])

    def test_config_reports_like_the_box_and_sends_effective_values(self):
        self.devices.replies = {"config": {"warnings": ["rounded"]}}
        out = io.StringIO()
        with patch.object(i2c_dispatcher, "_persist_params") as persist, \
                contextlib.redirect_stdout(out):
            I2CNet("lji2c", NETS[1]).config(frequency_hz=100_000)
        self.assertEqual(self.devices.calls, [
            ("i2c_hs", "lji2c", "config", (100_000, False), {})])
        persist.assert_called_once_with("lji2c", frequency_hz=100_000)
        self.assertIn("WARNING: rounded", out.getvalue())
        self.assertIn("I2C configured:", out.getvalue())

    def test_overrides_travel_with_every_later_call(self):
        self.devices.replies = {"scan": []}
        driver = i2c_dispatcher._resolve_net_and_driver(
            "lji2c", {"frequency_hz": 50_000, "mode": "ignored"})
        driver.scan()
        self.assertEqual(self.devices.calls[-1][3], (0x08, 0x77, {"frequency_hz": 50_000}))

    def test_a_driver_failure_is_the_dispatchers_error_class(self):
        self.devices.error = "[I2C] No ACK from device at 0x48."
        with self.assertRaises(I2CBackendError) as caught:
            I2CNet("lji2c", NETS[1]).read(address=0x48, num_bytes=2)
        self.assertIn("No ACK from device at 0x48", str(caught.exception))


class AdapterTests(unittest.TestCase):
    def test_adapters_take_the_arguments_the_stand_ins_send(self):
        import inspect

        from lager import i2c_hs, spi_hs
        expected = {
            (spi_hs.SPIHardwareAdapter, "config"): ["cfg"],
            (spi_hs.SPIHardwareAdapter, "read"): ["n_words", "fill", "keep_cs", "overrides"],
            (spi_hs.SPIHardwareAdapter, "read_write"): ["data", "keep_cs", "overrides"],
            (spi_hs.SPIHardwareAdapter, "transfer"):
                ["data", "n_words", "fill", "keep_cs", "overrides"],
            (i2c_hs.I2CHardwareAdapter, "config"): ["frequency_hz", "pull_ups"],
            (i2c_hs.I2CHardwareAdapter, "scan"): ["start_addr", "end_addr", "overrides"],
            (i2c_hs.I2CHardwareAdapter, "read"): ["address", "num_bytes", "overrides"],
            (i2c_hs.I2CHardwareAdapter, "write"): ["address", "data", "overrides"],
            (i2c_hs.I2CHardwareAdapter, "write_read"):
                ["address", "data", "num_bytes", "overrides"],
        }
        for (adapter, method), names in expected.items():
            params = list(inspect.signature(getattr(adapter, method)).parameters)[1:]
            self.assertEqual(params, names, (adapter.__name__, method))


class NetFactorySwitchTests(unittest.TestCase):
    """The one Linux file this touches: default off."""

    def setUp(self):
        from lager.nets import net as net_module
        self.net_module = net_module
        patcher = patch.object(net_module.Net, "get_local_nets",
                               classmethod(lambda cls: list(NETS)))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(bus_proxy, "install")
        self.install = patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self, value, name, role):
        env = {k: v for k, v in os.environ.items() if k != "LAGER_HOSTLINK"}
        if value is not None:
            env["LAGER_HOSTLINK"] = value
        with patch.dict(os.environ, env, clear=True):
            return self.net_module.Net.get(name, getattr(self.net_module.NetType, role))

    def test_unset_or_any_other_value_leaves_the_dispatchers_alone(self):
        for value in (None, "0", "", "true"):
            self.assertIsInstance(self._get(value, "ljspi", "SPI"), SPINet)
            self.assertIsInstance(self._get(value, "lji2c", "I2C"), I2CNet)
        self.install.assert_not_called()

    def test_one_installs_the_stand_ins_and_returns_the_same_net_classes(self):
        self.assertIs(type(self._get("1", "ljspi", "SPI")), SPINet)
        self.assertIs(type(self._get("1", "lji2c", "I2C")), I2CNet)
        self.assertEqual(self.install.call_count, 2)

    def test_one_does_not_install_for_other_roles(self):
        with patch("lager.hostlink.io_proxy.Device"):
            self._get("1", "ljio", "GPIO")
        self.install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
