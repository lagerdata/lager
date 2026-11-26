# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""What a script's hostlink stand-ins send to the hardware service.

On a Box whose container has no USB, a script's GPIO, ADC, DAC, SPI and I2C
nets are stand-ins that call the hardware service's adapters (``gpio_hs``
and the rest). The service caches one adapter per ``(device, address)`` when
the request carries an address, and each adapter is bound to the one net
name it was built with. So a stand-in that sends the saved record, address
and all, makes every net on one instrument share the first net's adapter.

These tests pin the shape the stand-ins send -- the net's own name, the
instrument and the physical ``device_id``, never the address -- to the shape
``lager gpo`` and friends already send (``net_command._proxy``), and drive
several nets on one shared address through a stand-in hardware service that
uses the real cache rule and the real adapters.
"""
import contextlib
import importlib
import unittest
from unittest.mock import patch

from lager import hardware_service
from lager.exceptions import I2CBackendError, SPIBackendError
from lager.hostlink import bus_proxy, io_proxy
from lager.http_handlers import net_command
from lager.safety import resolve_net_name

# One four-channel FTDI; every net on it is saved with the same address.
FTDI_ADDRESS = "USB0::0x0403::0x6011::FT0001::INSTR"
FTDI_DEVICE_ID = "ft232h:FT0001"
# One LabJack U3, likewise shared by several analog nets.
U3_ADDRESS = "USB0::0x0CD5::0x0003::port-1-1.3::INSTR"


def _ftdi_gpio(name, pin, interface):
    return {"name": name, "role": "gpio", "instrument": "FTDI_FT232H",
            "address": FTDI_ADDRESS, "pin": pin, "params": {"interface": interface}}


def _u3(name, role, pin):
    return {"name": name, "role": role, "instrument": "LabJack_U3",
            "address": U3_ADDRESS, "pin": pin}


def _ftdi_bus(name, role, interface):
    return {"name": name, "role": role, "instrument": "FTDI_FT232H",
            "address": FTDI_ADDRESS, "params": {"interface": interface, "word_size": 8}}


SIX_GPIO = [_ftdi_gpio("SEL_%s" % letter, str(pin), 2)
            for pin, letter in enumerate("ABCDEF", start=1)]


class _HardwareService:
    """The hardware service as far as these stand-ins see it.

    Uses the service's own cache rule and builds the service's own adapters,
    so a request that names the wrong net reaches the wrong adapter exactly
    as it does in the agent. Each ``Device`` call is one /invoke.
    """

    def __init__(self, testcase, *modules):
        self.cache = {}
        self.sent = []
        service = self

        class Device:
            def __init__(self, device_name, net_info=None, timeout=None):
                self.device_name, self.net_info = device_name, net_info

            def __getattr__(self, function):
                def call(*args, **kwargs):
                    return service.invoke(self.device_name, self.net_info,
                                          function, args, kwargs)
                return call

        for module in modules:
            patcher = patch.object(module, "Device", Device)
            patcher.start()
            testcase.addCleanup(patcher.stop)

    def invoke(self, device_name, net_info, function, args, kwargs):
        self.sent.append((device_name, dict(net_info)))
        # hardware_service.invoke(), verbatim.
        address = net_info.get('address') if net_info else None
        cache_key = ((device_name, address) if address
                     else (device_name, hardware_service.get_net_info_hash(net_info)))
        device = self.cache.get(cache_key)
        if device is None:
            device = importlib.import_module("lager." + device_name).create_device(net_info)
            self.cache[cache_key] = device
        return getattr(device, function)(*args, **kwargs)


class _Bench:
    """The instruments behind the adapters: one value per net, by name."""

    def __init__(self, testcase):
        self.levels = {}
        self.waited_on = []
        self.bus_calls = []
        bench = self

        class Pin:
            def __init__(self, netname):
                self.netname = netname

            def input(self):
                return bench.levels.get(self.netname, 0)

            def output(self, level):
                bench.levels[self.netname] = level

        class BusDriver:
            _word_size = 8
            clamp_warnings = []

            def __init__(self, netname):
                self.netname = netname

            def read_write(self, data, keep_cs=False):
                bench.bus_calls.append((self.netname, "read_write", list(data)))
                return list(data)

            def read(self, address, num_bytes):
                bench.bus_calls.append((self.netname, "read", address))
                return [0] * num_bytes

        @contextlib.contextmanager
        def driver_for_call(netname, overrides=None):
            yield BusDriver(netname)

        def wait_for_level(netname, level, **_):
            bench.waited_on.append(netname)
            return 0.5

        from lager.io.adc import dispatcher as adc
        from lager.io.dac import dispatcher as dac
        from lager.io.gpio import dispatcher as gpio
        from lager.protocols.i2c import dispatcher as i2c
        from lager.protocols.spi import dispatcher as spi
        patches = [
            patch.object(gpio, "gpi", lambda netname: bench.levels.get(netname, 0)),
            patch.object(gpio._dispatcher, "_resolve_net_and_driver",
                         lambda netname: (Pin(netname), None)),
            patch.object(gpio, "wait_for_level", wait_for_level),
            patch.object(adc, "read", lambda netname, **_: bench.levels.get(netname, 0.0)),
            patch.object(dac, "read_voltage", lambda netname: bench.levels.get(netname, 0.0)),
            patch.object(dac, "write_voltage",
                         lambda netname, v: bench.levels.__setitem__(netname, v)),
            patch.object(spi, "_driver_for_call", driver_for_call),
            patch.object(i2c, "_driver_for_call", driver_for_call),
        ]
        for patcher in patches:
            patcher.start()
            testcase.addCleanup(patcher.stop)


class SentShapeTests(unittest.TestCase):
    def setUp(self):
        self.service = _HardwareService(self, io_proxy, bus_proxy)
        _Bench(self)

    def test_each_gpio_sends_its_own_name_and_the_shared_device_id_only(self):
        a, b = _ftdi_gpio("SEL_A", "1", 2), _ftdi_gpio("SEL_B", "5", 3)
        io_proxy.for_role("gpio", "SEL_A", a).input()
        io_proxy.for_role("gpio", "SEL_B", b).input()
        sent = [net_info for _, net_info in self.service.sent]
        self.assertEqual(sent, [
            {"name": "SEL_A", "instrument": "FTDI_FT232H", "device_id": FTDI_DEVICE_ID},
            {"name": "SEL_B", "instrument": "FTDI_FT232H", "device_id": FTDI_DEVICE_ID}])

    def test_the_stand_in_keeps_its_pin_from_the_saved_record(self):
        gpio = io_proxy.for_role("gpio", "SEL_B", _ftdi_gpio("SEL_B", "5", 3))
        self.assertEqual(gpio.pin, "5")

    def test_wait_for_level_sends_the_same_shape(self):
        gpio = io_proxy.for_role("gpio", "SEL_A", _ftdi_gpio("SEL_A", "1", 2))
        gpio.input()
        gpio.wait_for_level(1, timeout=5)
        first, second = (net_info for _, net_info in self.service.sent)
        self.assertEqual(first, second)
        self.assertNotIn("address", second)

    def test_analog_and_bus_stand_ins_send_no_address(self):
        io_proxy.for_role("adc", "ain0", _u3("ain0", "adc", "AIN0")).input()
        io_proxy.for_role("dac", "dac0", _u3("dac0", "dac", "DAC0")).input()
        bus_proxy.HostSPIDriver(_ftdi_bus("spi_a", "spi", 0), SPIBackendError).write([1])
        bus_proxy.HostI2CDriver(_ftdi_bus("i2c_b", "i2c", 1), I2CBackendError).read(0x48, 1)
        for device_name, net_info in self.service.sent:
            self.assertEqual(set(net_info), {"name", "instrument", "device_id"}, device_name)

    def test_the_safety_interlock_still_finds_the_net(self):
        io_proxy.for_role("gpio", "SEL_C", _ftdi_gpio("SEL_C", "3", 2)).input()
        self.assertEqual(resolve_net_name(self.service.sent[0][1]), "SEL_C")


class SharedAddressTests(unittest.TestCase):
    """Several nets on one instrument each reach their own pin."""

    def setUp(self):
        self.service = _HardwareService(self, io_proxy, bus_proxy)
        self.bench = _Bench(self)

    def test_two_gpio_nets_read_back_what_each_was_given(self):
        a = io_proxy.for_role("gpio", "SEL_A", _ftdi_gpio("SEL_A", "1", 2))
        b = io_proxy.for_role("gpio", "SEL_B", _ftdi_gpio("SEL_B", "5", 3))
        a.output(0)
        b.output(1)
        self.assertEqual((a.input(), b.input()), (0, 1))

    def test_six_nets_on_one_ftdi_round_trip(self):
        levels = {"SEL_A": 0, "SEL_B": 1, "SEL_C": 0, "SEL_D": 0, "SEL_E": 1, "SEL_F": 1}
        nets = {rec["name"]: io_proxy.for_role("gpio", rec["name"], rec) for rec in SIX_GPIO}
        for name, level in levels.items():
            nets[name].output(level)
        self.assertEqual({n: int(g.input()) for n, g in nets.items()}, levels)
        self.assertEqual(self.bench.levels, levels)

    def test_a_later_script_still_reaches_its_own_net(self):
        # The service outlives a script: its cache is not cleared between them.
        io_proxy.for_role("gpio", "SEL_A", SIX_GPIO[0]).output(1)
        later = io_proxy.for_role("gpio", "SEL_F", SIX_GPIO[5])
        later.output(0)
        self.assertEqual(self.bench.levels, {"SEL_A": 1, "SEL_F": 0})

    def test_wait_for_level_waits_on_its_own_net(self):
        io_proxy.for_role("gpio", "SEL_A", SIX_GPIO[0]).input()
        io_proxy.for_role("gpio", "SEL_D", SIX_GPIO[3]).wait_for_level(1, timeout=5)
        self.assertEqual(self.bench.waited_on, ["SEL_D"])

    def test_adc_nets_on_one_labjack(self):
        self.bench.levels.update({"ain0": 1.25, "ain1": 2.5})
        ain0 = io_proxy.for_role("adc", "ain0", _u3("ain0", "adc", "AIN0"))
        ain1 = io_proxy.for_role("adc", "ain1", _u3("ain1", "adc", "AIN1"))
        self.assertEqual((ain0.input(), ain1.input()), (1.25, 2.5))

    def test_dac_nets_on_one_labjack(self):
        dac0 = io_proxy.for_role("dac", "dac0", _u3("dac0", "dac", "DAC0"))
        dac1 = io_proxy.for_role("dac", "dac1", _u3("dac1", "dac", "DAC1"))
        dac0.output(1.0)
        dac1.output(3.0)
        self.assertEqual((dac0.get_voltage(), dac1.get_voltage()), (1.0, 3.0))

    def test_spi_nets_on_one_ftdi(self):
        bus_proxy.HostSPIDriver(_ftdi_bus("spi_a", "spi", 0), SPIBackendError).write([1])
        bus_proxy.HostSPIDriver(_ftdi_bus("spi_b", "spi", 1), SPIBackendError).write([2])
        self.assertEqual([c[0] for c in self.bench.bus_calls], ["spi_a", "spi_b"])

    def test_i2c_nets_on_one_ftdi(self):
        bus_proxy.HostI2CDriver(_ftdi_bus("i2c_a", "i2c", 0), I2CBackendError).read(0x48, 1)
        bus_proxy.HostI2CDriver(_ftdi_bus("i2c_b", "i2c", 1), I2CBackendError).read(0x50, 1)
        self.assertEqual([c[0] for c in self.bench.bus_calls], ["i2c_a", "i2c_b"])


class SameAsTheCommandRouteTests(unittest.TestCase):
    """A script and ``lager gpo`` (and the rest) name a device identically,
    so both take the same per-device lock in the hardware service."""

    RECORDS = [
        ("gpio", _ftdi_gpio("SEL_A", "1", 2)),
        ("gpio", _u3("fio4", "gpio", "FIO4")),
        ("adc", _u3("ain0", "adc", "AIN0")),
        ("dac", _u3("dac0", "dac", "DAC0")),
        ("spi", _ftdi_bus("spi_a", "spi", 0)),
        ("i2c", _ftdi_bus("i2c_a", "i2c", 1)),
    ]

    def _command_route_sends(self, role, rec):
        sent = []

        def device(device_name, net_info=None, timeout=None):
            sent.append(net_info)

        with patch.object(net_command, "_find_rec", lambda *_: rec), \
                patch.object(net_command, "Device", device):
            net_command._proxy(rec["name"], role)
        return sent[0]

    def _script_sends(self, role, rec):
        service = _HardwareService(self, io_proxy, bus_proxy)
        _Bench(self)
        if role in ("spi", "i2c"):
            driver_class = bus_proxy.HostSPIDriver if role == "spi" else bus_proxy.HostI2CDriver
            driver = driver_class(rec, RuntimeError)
            driver.write([1]) if role == "spi" else driver.read(0x48, 1)
        else:
            io_proxy.for_role(role, rec["name"], rec).input()
        return service.sent[-1][1]

    def test_every_role_sends_what_the_command_route_sends(self):
        for role, rec in self.RECORDS:
            with self.subTest(role=role, net=rec["name"]):
                self.assertEqual(self._script_sends(role, rec),
                                 self._command_route_sends(role, rec))


if __name__ == "__main__":
    unittest.main()
