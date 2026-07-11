# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the generic POST /net/command handler
(box/lager/http_handlers/net_command.py).

The box package has hardware-only dependencies (pyvisa, usb, labjack, …) that
only exist inside the Docker container. We stub them in sys.modules before
import so these tests run on any developer machine, then drive the Flask route
through its test client.

Every Tier-1 role now routes through hardware_service via a Device proxy
(POST :8080/invoke), the same single-owner path supply/battery use, so these
tests mock ``net_command.Device`` and assert the handler calls the right proxy
method and shapes the response. Config persistence for spi/i2c stays box-side,
so those tests also patch the dispatcher module.

Covered:
  - each Tier-1 role dispatches to the right Device method and shapes the response
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
    {"name": "gpi1", "role": "gpio", "instrument": "labjack_t7"},
    {"name": "adc1", "role": "adc", "instrument": "labjack_t7"},
    {"name": "dac1", "role": "dac", "instrument": "labjack_t7"},
    {"name": "tc1", "role": "thermocouple", "instrument": "phidget"},
    {"name": "watt1", "role": "watt-meter", "instrument": "joulescope"},
    {"name": "load1", "role": "eload", "instrument": "rigol_dl3021",
     "address": "USB0::0x1AB1::0x0E11::DL3A::INSTR"},
    {"name": "spi1", "role": "spi", "instrument": "labjack_t7"},
    {"name": "i2c1", "role": "i2c", "instrument": "labjack_t7"},
    {"name": "energy1", "role": "energy-analyzer", "instrument": "joulescope"},
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

    def _run(self, body, dev=None, nets=None):
        """POST with net_command.Net and net_command.Device both mocked.

        Returns (response, dev) so callers can assert on the Device proxy calls.
        """
        dev = MagicMock() if dev is None else dev
        with patch('lager.http_handlers.net_command.Net') as NetMock, \
                patch('lager.http_handlers.net_command.Device',
                      return_value=dev) as DeviceMock:
            NetMock.get_local_nets.return_value = nets or SAVED_NETS
            resp = self._post(body)
        self._DeviceMock = DeviceMock
        return resp, dev

    def _patch_dispatcher(self, dotted, **attrs):
        """Build a fake dispatcher module and patch it into sys.modules."""
        fake = types.ModuleType(dotted)
        for name, value in attrs.items():
            setattr(fake, name, value)
        return patch.dict(sys.modules, {dotted: fake}), fake

    # ----- gpio -----

    def test_gpio_input(self):
        dev = MagicMock()
        dev.input.return_value = 1
        r, dev = self._run({"netname": "gpi1", "action": "input"}, dev)
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["value"], 1)
        self.assertIn("HIGH", data["message"])
        dev.input.assert_called_once_with()

    def test_gpio_wait_for_level(self):
        dev = MagicMock()
        dev.wait_for_level.return_value = 0.25
        r, dev = self._run(
            {"netname": "gpi1", "action": "wait_for_level",
             "params": {"level": 1, "timeout": 5, "scan_rate": 1000}}, dev)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(data["value"], 0.25, places=4)
        dev.wait_for_level.assert_called_once_with(1, timeout=5.0, scan_rate=1000)

    def test_gpio_wait_for_level_timeout_is_502(self):
        # A device-side timeout surfaces from hardware_service as a DeviceError.
        dev = MagicMock()
        dev.wait_for_level.side_effect = net_command.DeviceError("no edge")
        r, _ = self._run(
            {"netname": "gpi1", "action": "wait_for_level",
             "params": {"level": 1, "timeout": 1}}, dev)
        self.assertEqual(r.status_code, 502)

    def test_gpio_output_toggle(self):
        # Toggle is resolved inside the gpio_hs adapter; the handler passes the
        # raw level through and reports the resulting level the adapter returns.
        dev = MagicMock()
        dev.output.return_value = 1
        r, dev = self._run({"netname": "gpi1", "action": "output",
                            "params": {"level": "toggle"}}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], 1)
        dev.output.assert_called_once_with("toggle")

    def test_gpio_output_high_level(self):
        dev = MagicMock()
        dev.output.return_value = 1
        r, dev = self._run({"netname": "gpi1", "action": "output",
                            "params": {"level": "high"}}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], 1)
        dev.output.assert_called_once_with("high")

    def test_gpio_output_high(self):
        dev = MagicMock()
        r, dev = self._run({"netname": "gpi1", "action": "output_high"}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        dev.output.assert_called_once_with(1)

    # ----- adc / dac -----

    def test_adc_read(self):
        dev = MagicMock()
        dev.input.return_value = 1.234567
        r, dev = self._run({"netname": "adc1", "action": "read"}, dev)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(data["value"], 1.234567, places=5)
        self.assertIn("V", data["message"])

    def test_dac_set(self):
        dev = MagicMock()
        r, dev = self._run({"netname": "dac1", "action": "set",
                            "params": {"value": 1.5}}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        dev.output.assert_called_once_with(1.5)

    def test_dac_set_missing_value_is_400(self):
        r, _ = self._run({"netname": "dac1", "action": "set"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["success"])

    # ----- thermocouple -----

    def test_thermocouple_read(self):
        dev = MagicMock()
        dev.read.return_value = 25.5
        r, dev = self._run({"netname": "tc1", "action": "read"}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], 25.5)
        dev.read.assert_called_once_with()

    # ----- watt-meter -----

    def test_watt_meter_read(self):
        dev = MagicMock()
        dev.measure.return_value = {"value": 0.5}
        r, dev = self._run({"netname": "watt1", "action": "read"}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], 0.5)
        dev.measure.assert_called_once_with("read", 0.1)

    def test_watt_meter_current(self):
        dev = MagicMock()
        dev.measure.return_value = {"value": 0.0123}
        r, dev = self._run({"netname": "watt1", "action": "current",
                            "params": {"duration": 0.5}}, dev)
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertAlmostEqual(data["value"], 0.0123, places=5)
        self.assertIn("A", data["message"])
        dev.measure.assert_called_once_with("current", 0.5)

    def test_watt_meter_all(self):
        dev = MagicMock()
        dev.measure.return_value = {"current": 0.1, "voltage": 3.3, "power": 0.33}
        r, dev = self._run({"netname": "watt1", "action": "all",
                            "params": {"duration": 1.0}}, dev)
        self.assertEqual(r.status_code, 200)
        value = r.get_json()["value"]
        self.assertAlmostEqual(value["current"], 0.1, places=5)
        self.assertAlmostEqual(value["voltage"], 3.3, places=3)
        self.assertAlmostEqual(value["power"], 0.33, places=3)
        dev.measure.assert_called_once_with("all", 1.0)

    # ----- eload -----

    def test_eload_state(self):
        dev = MagicMock()
        dev.get_state_dict.return_value = {
            "mode": "CC", "input_enabled": True,
            "measured_voltage": 3.30, "measured_current": 0.50,
            "measured_power": 1.65, "current_setting": 0.5,
        }
        r, dev = self._run({"netname": "load1", "action": "state"}, dev)
        self.assertEqual(r.status_code, 200)
        value = r.get_json()["value"]
        self.assertEqual(value["mode"], "CC")
        self.assertTrue(value["input_enabled"])
        self.assertAlmostEqual(value["current_setting"], 0.5, places=3)
        dev.get_state_dict.assert_called_once_with()
        # Routed to the right hardware_service module + VISA address.
        device_name, net_info = self._DeviceMock.call_args.args
        self.assertEqual(device_name, "rigol_dl3021")
        self.assertEqual(net_info["address"], "USB0::0x1AB1::0x0E11::DL3A::INSTR")

    def test_eload_cc_set(self):
        dev = MagicMock()
        dev.apply_setpoint.return_value = 0.5
        r, dev = self._run({"netname": "load1", "action": "cc",
                            "params": {"value": 0.5}}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        dev.apply_setpoint.assert_called_once_with("cc", 0.5)

    def test_eload_cc_read(self):
        dev = MagicMock()
        dev.read_setpoint.return_value = 0.5
        r, dev = self._run({"netname": "load1", "action": "cc"}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        dev.read_setpoint.assert_called_once_with("cc")

    def test_eload_unsupported_instrument_is_error(self):
        nets = [{"name": "load1", "role": "eload",
                 "instrument": "acme_9000", "address": "USB::x"}]
        with patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = nets
            r = self._post({"netname": "load1", "action": "cc",
                            "params": {"value": 0.5}})
        # ELoadBackendError (LagerBackendError) -> 502 by the dispatch loop.
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()["success"])

    # ----- spi -----

    def test_spi_transfer_pads_to_n_words(self):
        # Padding to n_words happens inside the spi_hs adapter; the handler
        # forwards the raw data + n_words and formats the returned words.
        dev = MagicMock()
        dev.transfer.return_value = {"words": [0xDE, 0xAD, 0xBE, 0xEF],
                                     "word_size": 8}
        r, dev = self._run({"netname": "spi1", "action": "transfer",
                            "params": {"data": [1, 2], "n_words": 4}}, dev)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"], [0xDE, 0xAD, 0xBE, 0xEF])
        self.assertEqual(data["message"], "DE AD BE EF")
        self.assertEqual(data["word_size"], 8)
        dev.transfer.assert_called_once_with([1, 2], 4, 0xFF, False, None)

    def test_spi_read(self):
        dev = MagicMock()
        dev.read.return_value = {"words": [1, 2, 3], "word_size": 8}
        r, dev = self._run({"netname": "spi1", "action": "read",
                            "params": {"n_words": 3}}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], [1, 2, 3])
        dev.read.assert_called_once_with(3, 0xFF, False, None)

    def test_spi_write_full_duplex(self):
        dev = MagicMock()
        dev.read_write.return_value = {"words": [0x11, 0x22], "word_size": 8}
        r, dev = self._run({"netname": "spi1", "action": "write",
                            "params": {"data": [0x11, 0x22]}}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], [0x11, 0x22])
        dev.read_write.assert_called_once_with([0x11, 0x22], False, None)

    def test_spi_config_persists_and_returns_effective(self):
        helpers = MagicMock()
        helpers.find_saved_net.return_value = {"name": "spi1", "params": {}}
        effective = {"mode": 0, "bit_order": "msb", "frequency_hz": 2_000_000,
                     "word_size": 8, "cs_active": "low", "cs_mode": "auto"}
        ctx, fake = self._patch_dispatcher(
            'lager.protocols.spi.dispatcher',
            _persist_params=MagicMock(),
            _get_spi_params=MagicMock(return_value=effective),
            helpers=helpers,
            SPIBackendError=type('SPIBackendError', (Exception,), {}))
        dev = MagicMock()
        with ctx:
            r, dev = self._run({"netname": "spi1", "action": "config",
                                "params": {"frequency_hz": 2_000_000}}, dev)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"]["frequency_hz"], 2_000_000)
        dev.config.assert_called_once_with({"frequency_hz": 2_000_000})
        fake._persist_params.assert_called_once_with("spi1", frequency_hz=2_000_000)

    def test_spi_config_rejects_bad_frequency(self):
        r, _ = self._run({"netname": "spi1", "action": "config",
                          "params": {"frequency_hz": 0}})
        self.assertEqual(r.status_code, 400)

    def test_spi_known_role_unknown_action_is_400_not_501(self):
        # The control plane treats 501 as "use the /python fallback"; a supported
        # role with an unsupported action must be a 400, never a 501.
        r, _ = self._run({"netname": "spi1", "action": "bogus"})
        self.assertEqual(r.status_code, 400)

    def test_spi_hardware_error_is_502(self):
        dev = MagicMock()
        dev.read.side_effect = net_command.DeviceError("bus fault")
        r, _ = self._run({"netname": "spi1", "action": "read",
                          "params": {"n_words": 2}}, dev)
        self.assertEqual(r.status_code, 502)

    # ----- i2c -----

    def test_i2c_scan_with_devices(self):
        dev = MagicMock()
        dev.scan.return_value = [0x48, 0x50]
        r, dev = self._run({"netname": "i2c1", "action": "scan"}, dev)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"], [0x48, 0x50])
        self.assertIn("Found 2 device(s)", data["message"])
        self.assertIn("0x48", data["message"])
        dev.scan.assert_called_once_with(None, None, None)

    def test_i2c_scan_empty(self):
        dev = MagicMock()
        dev.scan.return_value = []
        r, dev = self._run({"netname": "i2c1", "action": "scan"}, dev)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"], [])
        self.assertEqual(data["message"], "No devices found")

    def test_i2c_read(self):
        dev = MagicMock()
        dev.read.return_value = [0xAB, 0xCD]
        r, dev = self._run({"netname": "i2c1", "action": "read",
                            "params": {"address": 0x48, "num_bytes": 2}}, dev)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"], [0xAB, 0xCD])
        self.assertEqual(data["message"], "AB CD")
        dev.read.assert_called_once_with(0x48, 2, None)

    def test_i2c_write(self):
        dev = MagicMock()
        r, dev = self._run({"netname": "i2c1", "action": "write",
                            "params": {"address": 0x50, "data": [1, 2, 3]}}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Wrote 3 byte(s) to 0x50", r.get_json()["message"])
        dev.write.assert_called_once_with(0x50, [1, 2, 3], None)

    def test_i2c_transfer(self):
        dev = MagicMock()
        dev.write_read.return_value = [0x99]
        r, dev = self._run({"netname": "i2c1", "action": "transfer",
                            "params": {"address": 0x48, "data": [7],
                                       "num_bytes": 1}}, dev)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], [0x99])
        dev.write_read.assert_called_once_with(0x48, [7], 1, None)

    def test_i2c_config_persists_and_returns_effective(self):
        helpers = MagicMock()
        helpers.find_saved_net.return_value = {
            "name": "i2c1", "params": {"frequency_hz": 100_000}}
        ctx, fake = self._patch_dispatcher(
            'lager.protocols.i2c.dispatcher',
            _persist_params=MagicMock(),
            helpers=helpers,
            I2CBackendError=type('I2CBackendError', (Exception,), {}))
        dev = MagicMock()
        with ctx:
            r, dev = self._run({"netname": "i2c1", "action": "config",
                                "params": {"frequency_hz": 400_000,
                                           "pull_ups": True}}, dev)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"]["frequency_hz"], 400_000)
        self.assertTrue(data["value"]["pull_ups"])
        dev.config.assert_called_once_with(400_000, True)
        fake._persist_params.assert_called_once_with(
            "i2c1", frequency_hz=400_000, pull_ups=True)

    def test_i2c_scan_with_range(self):
        dev = MagicMock()
        dev.scan.return_value = [0x10]
        r, dev = self._run({"netname": "i2c1", "action": "scan",
                            "params": {"start_addr": 0x08, "end_addr": 0x20}}, dev)
        self.assertEqual(r.status_code, 200)
        dev.scan.assert_called_once_with(0x08, 0x20, None)

    def test_i2c_read_missing_param_is_400(self):
        r, _ = self._run({"netname": "i2c1", "action": "read",
                          "params": {"address": 0x48}})
        self.assertEqual(r.status_code, 400)

    # ----- energy-analyzer -----

    def test_energy_read_stats(self):
        dev = MagicMock()
        dev.measure.return_value = {
            "current": {"mean": 0.001234},
            "voltage": {"mean": 3.3},
            "power": {"mean": 0.004072},
        }
        r, dev = self._run({"netname": "energy1", "action": "read_stats",
                            "params": {"duration": 2.0}}, dev)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertIn("I 0.001234 A", data["message"])
        self.assertIn("V 3.300 V", data["message"])
        dev.measure.assert_called_once_with("read_stats", 2.0)

    def test_energy_read_energy_long_duration_not_over_clamped(self):
        # Direct :9000 callers may integrate up to 120s (the old :5000 CLI
        # budget); the 30s clamp belongs only to Stout's Nginx-proxied path.
        dev = MagicMock()
        dev.measure.return_value = {
            "energy_j": 1.5, "charge_c": 0.5, "duration_s": 99.0}
        r, dev = self._run({"netname": "energy1", "action": "read_energy",
                            "params": {"duration": 99}}, dev)
        self.assertEqual(r.status_code, 200)
        dev.measure.assert_called_once_with("read_energy", 99.0)

    def test_energy_read_energy_clamps_max_duration(self):
        dev = MagicMock()
        dev.measure.return_value = {
            "energy_j": 1.5, "charge_c": 0.5, "duration_s": 120.0}
        r, dev = self._run({"netname": "energy1", "action": "read_energy",
                            "params": {"duration": 999}}, dev)
        self.assertEqual(r.status_code, 200)
        # 999s exceeds the 120s cap -> clamped to 120.0
        dev.measure.assert_called_once_with("read_energy", 120.0)

    def test_energy_read_stats_clamps_min_duration(self):
        dev = MagicMock()
        dev.measure.return_value = {}
        r, dev = self._run({"netname": "energy1", "action": "read_stats",
                            "params": {"duration": 0.001}}, dev)
        self.assertEqual(r.status_code, 200)
        # 0.001s is below the 0.1s floor -> clamped to 0.1
        dev.measure.assert_called_once_with("read_stats", 0.1)

    def test_energy_unknown_action_is_400(self):
        r, _ = self._run({"netname": "energy1", "action": "bogus"})
        self.assertEqual(r.status_code, 400)

    # ----- generic dispatch -----

    def test_unknown_action_is_400(self):
        r, _ = self._run({"netname": "adc1", "action": "output_high"})
        self.assertEqual(r.status_code, 400)

    def test_unsupported_role_is_501(self):
        with patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "scope1", "action": "enable"})
        self.assertEqual(r.status_code, 501)

    def test_unknown_net_is_404(self):
        with patch('lager.http_handlers.net_command.Net') as NetMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            r = self._post({"netname": "nope", "action": "read"})
        self.assertEqual(r.status_code, 404)

    def test_hardware_error_is_502(self):
        dev = MagicMock()
        dev.input.side_effect = net_command.DeviceError("Resource busy")
        r, _ = self._run({"netname": "adc1", "action": "read"}, dev)
        self.assertEqual(r.status_code, 502)

    def test_missing_fields_is_400(self):
        r = self._post({"netname": "adc1"})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
