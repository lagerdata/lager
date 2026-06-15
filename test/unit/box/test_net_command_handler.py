# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the generic POST /net/command handler
(box/lager/http_handlers/net_command.py).

The box package has hardware-only dependencies (pyvisa, usb, labjack, …) that
only exist inside the Docker container. We stub them in sys.modules before
import so these tests run on any developer machine, then drive the Flask route
through its test client while mocking the Net layer (no real hardware).

Covered:
  - each Tier-1 role dispatches to the right Net method and shapes the response
  - role is resolved from saved_nets.json; unknown net → 404
  - unsupported role → 501; unknown/!allowed action → 400; missing value → 400
  - hardware errors (DeviceError) → 502
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted: str) -> None:
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


_HARDWARE_STUBS = [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'pigpio', 'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
    # http_handlers/__init__ imports .uart, which needs flask_socketio.
    'flask_socketio',
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

sys.modules['simplejson'] = sys.modules['json']  # type: ignore[assignment]

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from flask import Flask  # noqa: E402
from lager.http_handlers import net_command  # noqa: E402


SAVED_NETS = [
    {"name": "gpi1", "role": "gpio"},
    {"name": "adc1", "role": "adc"},
    {"name": "dac1", "role": "dac"},
    {"name": "tc1", "role": "thermocouple"},
    {"name": "watt1", "role": "watt-meter"},
    {"name": "load1", "role": "eload"},
    {"name": "spi1", "role": "spi"},
    {"name": "i2c1", "role": "i2c"},
    {"name": "energy1", "role": "energy-analyzer"},
    {"name": "scope1", "role": "scope"},  # unsupported by /net/command
]


def _make_client():
    app = Flask(__name__)
    net_command.register_net_command_routes(app)
    return app.test_client()


class TestNetCommandHandler(unittest.TestCase):

    def setUp(self):
        self.client = _make_client()

    def _post(self, body):
        return self.client.post('/net/command', json=body)

    @patch('lager.http_handlers.net_command.Net')
    def test_gpio_input(self, NetMock):
        NetMock.get_local_nets.return_value = SAVED_NETS
        dev = MagicMock()
        dev.input.return_value = 1
        NetMock.get.return_value = dev

        r = self._post({"netname": "gpi1", "action": "input"})

        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["value"], 1)
        self.assertIn("HIGH", data["message"])
        dev.input.assert_called()

    @patch('lager.http_handlers.net_command.Net')
    def test_gpio_output_high(self, NetMock):
        NetMock.get_local_nets.return_value = SAVED_NETS
        dev = MagicMock()
        NetMock.get.return_value = dev

        r = self._post({"netname": "gpi1", "action": "output_high"})

        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        dev.output.assert_called_once_with(1)

    @patch('lager.http_handlers.net_command.Net')
    def test_adc_read(self, NetMock):
        NetMock.get_local_nets.return_value = SAVED_NETS
        dev = MagicMock()
        dev.input.return_value = 1.234567
        NetMock.get.return_value = dev

        r = self._post({"netname": "adc1", "action": "read"})

        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(data["value"], 1.234567, places=5)
        self.assertIn("V", data["message"])

    @patch('lager.http_handlers.net_command.Net')
    def test_dac_set(self, NetMock):
        NetMock.get_local_nets.return_value = SAVED_NETS
        dev = MagicMock()
        NetMock.get.return_value = dev

        r = self._post({"netname": "dac1", "action": "set", "params": {"value": 1.5}})

        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        dev.output.assert_called_once_with(1.5)

    @patch('lager.http_handlers.net_command.Net')
    def test_dac_set_missing_value_is_400(self, NetMock):
        NetMock.get_local_nets.return_value = SAVED_NETS
        NetMock.get.return_value = MagicMock()

        r = self._post({"netname": "dac1", "action": "set"})

        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["success"])

    @patch('lager.http_handlers.net_command.Net')
    def test_thermocouple_read(self, NetMock):
        NetMock.get_local_nets.return_value = SAVED_NETS
        dev = MagicMock()
        dev.read.return_value = 25.5
        NetMock.get.return_value = dev

        r = self._post({"netname": "tc1", "action": "read"})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], 25.5)

    @patch('lager.http_handlers.net_command.Net')
    def test_watt_meter_read_closes(self, NetMock):
        NetMock.get_local_nets.return_value = SAVED_NETS
        dev = MagicMock()
        dev.read.return_value = 0.5
        NetMock.get.return_value = dev

        r = self._post({"netname": "watt1", "action": "read"})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], 0.5)
        dev.close.assert_called_once()

    def test_eload_cc_set(self):
        fake = types.ModuleType('lager.power.eload.dispatcher')
        fake.set_constant_current = MagicMock(return_value={"mode": "CC", "current": 0.5})
        for n in ('get_constant_current', 'set_constant_voltage', 'get_constant_voltage',
                  'set_constant_resistance', 'get_constant_resistance',
                  'set_constant_power', 'get_constant_power'):
            setattr(fake, n, MagicMock(return_value={}))
        with patch.dict(sys.modules, {'lager.power.eload.dispatcher': fake}), \
                patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "load1", "action": "cc", "params": {"value": 0.5}})

        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        fake.set_constant_current.assert_called_once_with("load1", 0.5)

    # ----- spi -----

    def _patch_dispatcher(self, dotted, **attrs):
        """Build a fake dispatcher module and patch it into sys.modules."""
        fake = types.ModuleType(dotted)
        for name, value in attrs.items():
            setattr(fake, name, value)
        return patch.dict(sys.modules, {dotted: fake}), fake

    def test_spi_transfer_pads_to_n_words(self):
        drv = MagicMock()
        drv.read_write.return_value = [0xDE, 0xAD, 0xBE, 0xEF]
        ctx, fake = self._patch_dispatcher(
            'lager.protocols.spi.dispatcher',
            _resolve_net_and_driver=MagicMock(return_value=drv))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "spi1", "action": "transfer",
                            "params": {"data": [1, 2], "n_words": 4}})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"], [0xDE, 0xAD, 0xBE, 0xEF])
        self.assertEqual(data["message"], "DE AD BE EF")
        # data padded to n_words=4 with fill 0xFF
        drv.read_write.assert_called_once_with([1, 2, 0xFF, 0xFF])

    def test_spi_read(self):
        drv = MagicMock()
        drv.read.return_value = [1, 2, 3]
        ctx, _ = self._patch_dispatcher(
            'lager.protocols.spi.dispatcher',
            _resolve_net_and_driver=MagicMock(return_value=drv))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "spi1", "action": "read",
                            "params": {"n_words": 3}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], [1, 2, 3])
        drv.read.assert_called_once_with(3, fill=0xFF)

    def test_spi_known_role_unknown_action_is_400_not_501(self):
        # Stout treats 501 as "use the /python fallback"; a supported role with
        # an unsupported action must be a 400, never a 501.
        ctx, _ = self._patch_dispatcher(
            'lager.protocols.spi.dispatcher',
            _resolve_net_and_driver=MagicMock(return_value=MagicMock()))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "spi1", "action": "bogus"})
        self.assertEqual(r.status_code, 400)

    def test_spi_hardware_error_is_502(self):
        ctx, _ = self._patch_dispatcher(
            'lager.protocols.spi.dispatcher',
            _resolve_net_and_driver=MagicMock(
                side_effect=net_command.LagerBackendError("bus fault")))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "spi1", "action": "read",
                            "params": {"n_words": 2}})
        self.assertEqual(r.status_code, 502)

    # ----- i2c -----

    def test_i2c_scan_with_devices(self):
        drv = MagicMock()
        drv.scan.return_value = [0x48, 0x50]
        ctx, _ = self._patch_dispatcher(
            'lager.protocols.i2c.dispatcher',
            _resolve_net_and_driver=MagicMock(return_value=drv))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "i2c1", "action": "scan"})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"], [0x48, 0x50])
        self.assertIn("Found 2 device(s)", data["message"])
        self.assertIn("0x48", data["message"])

    def test_i2c_scan_empty(self):
        drv = MagicMock()
        drv.scan.return_value = []
        ctx, _ = self._patch_dispatcher(
            'lager.protocols.i2c.dispatcher',
            _resolve_net_and_driver=MagicMock(return_value=drv))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "i2c1", "action": "scan"})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"], [])
        self.assertEqual(data["message"], "No devices found")

    def test_i2c_read(self):
        drv = MagicMock()
        drv.read.return_value = [0xAB, 0xCD]
        ctx, _ = self._patch_dispatcher(
            'lager.protocols.i2c.dispatcher',
            _resolve_net_and_driver=MagicMock(return_value=drv))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "i2c1", "action": "read",
                            "params": {"address": 0x48, "num_bytes": 2}})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"], [0xAB, 0xCD])
        self.assertEqual(data["message"], "AB CD")
        drv.read.assert_called_once_with(0x48, 2)

    def test_i2c_write(self):
        drv = MagicMock()
        ctx, _ = self._patch_dispatcher(
            'lager.protocols.i2c.dispatcher',
            _resolve_net_and_driver=MagicMock(return_value=drv))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "i2c1", "action": "write",
                            "params": {"address": 0x50, "data": [1, 2, 3]}})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Wrote 3 byte(s) to 0x50", r.get_json()["message"])
        drv.write.assert_called_once_with(0x50, [1, 2, 3])

    def test_i2c_transfer(self):
        drv = MagicMock()
        drv.write_read.return_value = [0x99]
        ctx, _ = self._patch_dispatcher(
            'lager.protocols.i2c.dispatcher',
            _resolve_net_and_driver=MagicMock(return_value=drv))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "i2c1", "action": "transfer",
                            "params": {"address": 0x48, "data": [7], "num_bytes": 1}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], [0x99])
        drv.write_read.assert_called_once_with(0x48, [7], 1)

    def test_i2c_read_missing_param_is_400(self):
        ctx, _ = self._patch_dispatcher(
            'lager.protocols.i2c.dispatcher',
            _resolve_net_and_driver=MagicMock(return_value=MagicMock()))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "i2c1", "action": "read",
                            "params": {"address": 0x48}})
        self.assertEqual(r.status_code, 400)

    # ----- energy-analyzer -----

    def test_energy_read_stats(self):
        ctx, fake = self._patch_dispatcher(
            'lager.measurement.energy_analyzer.dispatcher',
            read_stats=MagicMock(return_value={
                "current": {"mean": 0.001234},
                "voltage": {"mean": 3.3},
                "power": {"mean": 0.004072},
            }),
            read_energy=MagicMock(return_value={}))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "energy1", "action": "read_stats",
                            "params": {"duration": 2.0}})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertIn("I 0.001234 A", data["message"])
        self.assertIn("V 3.300 V", data["message"])
        fake.read_stats.assert_called_once_with("energy1", 2.0)

    def test_energy_read_energy_clamps_max_duration(self):
        ctx, fake = self._patch_dispatcher(
            'lager.measurement.energy_analyzer.dispatcher',
            read_energy=MagicMock(return_value={
                "energy_j": 1.5, "charge_c": 0.5, "duration_s": 30.0}),
            read_stats=MagicMock(return_value={}))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            # 99s exceeds the 30s cap -> clamped to 30.0
            r = self._post({"netname": "energy1", "action": "read_energy",
                            "params": {"duration": 99}})
        self.assertEqual(r.status_code, 200)
        fake.read_energy.assert_called_once_with("energy1", 30.0)

    def test_energy_read_stats_clamps_min_duration(self):
        ctx, fake = self._patch_dispatcher(
            'lager.measurement.energy_analyzer.dispatcher',
            read_stats=MagicMock(return_value={}),
            read_energy=MagicMock(return_value={}))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            # 0.001s is below the 0.1s floor -> clamped to 0.1
            r = self._post({"netname": "energy1", "action": "read_stats",
                            "params": {"duration": 0.001}})
        self.assertEqual(r.status_code, 200)
        fake.read_stats.assert_called_once_with("energy1", 0.1)

    def test_energy_unknown_action_is_400(self):
        ctx, _ = self._patch_dispatcher(
            'lager.measurement.energy_analyzer.dispatcher',
            read_stats=MagicMock(return_value={}),
            read_energy=MagicMock(return_value={}))
        with ctx, patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "energy1", "action": "bogus"})
        self.assertEqual(r.status_code, 400)

    @patch('lager.http_handlers.net_command.Net')
    def test_unknown_action_is_400(self, NetMock):
        NetMock.get_local_nets.return_value = SAVED_NETS
        NetMock.get.return_value = MagicMock()

        r = self._post({"netname": "adc1", "action": "output_high"})

        self.assertEqual(r.status_code, 400)

    @patch('lager.http_handlers.net_command.Net')
    def test_unsupported_role_is_501(self, NetMock):
        NetMock.get_local_nets.return_value = SAVED_NETS

        r = self._post({"netname": "scope1", "action": "enable"})

        self.assertEqual(r.status_code, 501)

    @patch('lager.http_handlers.net_command.Net')
    def test_unknown_net_is_404(self, NetMock):
        NetMock.get_local_nets.return_value = SAVED_NETS

        r = self._post({"netname": "nope", "action": "read"})

        self.assertEqual(r.status_code, 404)

    @patch('lager.http_handlers.net_command.Net')
    def test_hardware_error_is_502(self, NetMock):
        NetMock.get_local_nets.return_value = SAVED_NETS
        dev = MagicMock()
        dev.input.side_effect = net_command.DeviceError("Resource busy")
        NetMock.get.return_value = dev

        r = self._post({"netname": "adc1", "action": "read"})

        self.assertEqual(r.status_code, 502)

    def test_missing_fields_is_400(self):
        r = self._post({"netname": "adc1"})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
