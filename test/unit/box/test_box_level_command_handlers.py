# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the box-level command handlers
(box/lager/http_handlers/{ble,wifi,blufi}.py — POST /ble|wifi|blufi/command).

These endpoints drive the box's own hardware (Bluetooth adapter, wlan
interface) rather than a saved net. The bleak/nmcli/BlufiClient layers are
mocked: ``run_bleak`` is replaced so no event loop or adapter is touched,
the ``lager.protocols.wifi`` functions are patched, and ``lager.blufi``
is replaced with a fake module exposing a scripted BlufiClient.

Covered per handler:
  - each action's request/response contract (params in, envelope out)
  - input validation -> 400
  - hardware/environment failures -> 502
  - unknown action -> 400
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
from lager.http_handlers import ble as ble_handler  # noqa: E402
from lager.http_handlers import wifi as wifi_handler  # noqa: E402
from lager.http_handlers import blufi as blufi_handler  # noqa: E402


def _fake_run_bleak(result):
    """A run_bleak stand-in: closes the coroutine (no loop) and returns
    a canned result (or raises it, if it's an exception)."""
    def run(coro, timeout):
        coro.close()
        if isinstance(result, Exception):
            raise result
        return result
    return run


# ---------------------------------------------------------------------------
# /ble/command
# ---------------------------------------------------------------------------

SCANNED = [
    {"name": "22:33:44:55:66:77", "address": "22:33:44:55:66:77",
     "rssi": -90, "uuids": []},
    {"name": "Zeta", "address": "AA:BB:CC:DD:EE:01", "rssi": -50,
     "uuids": ["0000180f-0000-1000-8000-00805f9b34fb"]},
    {"name": "alpha", "address": "AA:BB:CC:DD:EE:02", "rssi": -60,
     "uuids": []},
]


class TestBleHandler(unittest.TestCase):

    def setUp(self):
        app = Flask(__name__)
        ble_handler.register_ble_routes(app)
        self.client = app.test_client()

    def _post(self, body):
        return self.client.post('/ble/command', json=body)

    def test_scan_sorts_named_devices_first(self):
        with patch.object(ble_handler, 'run_bleak',
                          _fake_run_bleak(list(SCANNED))):
            r = self._post({"action": "scan", "params": {"timeout": 5}})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(data["success"])
        names = [d["name"] for d in data["value"]["devices"]]
        # Named devices (sorted) first, unnamed (name == address) last.
        self.assertEqual(names, ["Zeta", "alpha", "22:33:44:55:66:77"])
        self.assertIn("Found 3 device(s)", data["message"])

    def test_scan_name_filters(self):
        with patch.object(ble_handler, 'run_bleak',
                          _fake_run_bleak(list(SCANNED))):
            r = self._post({"action": "scan",
                            "params": {"name_contains": "alp"}})
        devices = r.get_json()["value"]["devices"]
        self.assertEqual([d["name"] for d in devices], ["alpha"])

        with patch.object(ble_handler, 'run_bleak',
                          _fake_run_bleak(list(SCANNED))):
            r = self._post({"action": "scan", "params": {"name_exact": "Zeta"}})
        devices = r.get_json()["value"]["devices"]
        self.assertEqual([d["name"] for d in devices], ["Zeta"])

    def test_scan_timeout_out_of_range_is_400(self):
        r = self._post({"action": "scan", "params": {"timeout": 999}})
        self.assertEqual(r.status_code, 400)

    def test_info_returns_gatt_services(self):
        result = {"address": "AA:BB:CC:DD:EE:01", "connected": True,
                  "services": [{"uuid": "u", "description": "d",
                                "characteristics": []}]}
        with patch.object(ble_handler, 'run_bleak', _fake_run_bleak(result)):
            r = self._post({"action": "info",
                            "params": {"address": "AA:BB:CC:DD:EE:01"}})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(data["value"]["connected"])
        self.assertEqual(len(data["value"]["services"]), 1)
        self.assertIn("1 service(s)", data["message"])

    def test_connect_is_info(self):
        result = {"address": "AA:BB:CC:DD:EE:01", "connected": True,
                  "services": []}
        with patch.object(ble_handler, 'run_bleak', _fake_run_bleak(result)):
            r = self._post({"action": "connect",
                            "params": {"address": "AA:BB:CC:DD:EE:01"}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["value"]["connected"])

    def test_bad_address_is_400(self):
        for action in ("info", "connect", "disconnect"):
            r = self._post({"action": action, "params": {"address": "nope"}})
            self.assertEqual(r.status_code, 400, action)

    def test_disconnect(self):
        result = {"address": "AA:BB:CC:DD:EE:01", "disconnected": True}
        with patch.object(ble_handler, 'run_bleak', _fake_run_bleak(result)):
            r = self._post({"action": "disconnect",
                            "params": {"address": "AA:BB:CC:DD:EE:01"}})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(data["value"]["disconnected"])

    def test_bleak_failure_is_502(self):
        with patch.object(ble_handler, 'run_bleak',
                          _fake_run_bleak(RuntimeError("adapter off"))):
            r = self._post({"action": "scan", "params": {}})
        self.assertEqual(r.status_code, 502)
        self.assertIn("BLE error", r.get_json()["error"])

    def test_unknown_action_is_400(self):
        r = self._post({"action": "bogus", "params": {}})
        self.assertEqual(r.status_code, 400)


# ---------------------------------------------------------------------------
# /wifi/command
# ---------------------------------------------------------------------------

class TestWifiHandler(unittest.TestCase):

    def setUp(self):
        app = Flask(__name__)
        wifi_handler.register_wifi_routes(app)
        self.client = app.test_client()

    def _post(self, body):
        return self.client.post('/wifi/command', json=body)

    def test_status_reports_connected_interface(self):
        interfaces = {
            "wlan0": {"interface": "wlan0", "ssid": "labnet",
                      "state": "Connected"},
            "wlan1": {"interface": "wlan1", "ssid": "Not Connected",
                      "state": "Disconnected"},
        }
        with patch('lager.protocols.wifi.get_wifi_status',
                   return_value=interfaces):
            r = self._post({"action": "status"})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(data["value"]["interfaces"]), 2)
        self.assertIn("Connected to labnet on wlan0", data["message"])

    def test_status_error_is_502(self):
        error = {"error": {"interface": "error", "ssid": "Error: no iwconfig",
                           "state": "Failed"}}
        with patch('lager.protocols.wifi.get_wifi_status', return_value=error):
            r = self._post({"action": "status"})
        self.assertEqual(r.status_code, 502)

    def test_scan_sorts_by_strength(self):
        result = {"access_points": [
            {"ssid": "weak", "strength": 20, "security": "Open"},
            {"ssid": "strong", "strength": 90, "security": "Secured"},
        ]}
        with patch('lager.protocols.wifi.scan_wifi',
                   return_value=result) as scan:
            r = self._post({"action": "scan", "params": {"interface": "wlan1"}})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        ssids = [n["ssid"] for n in data["value"]["access_points"]]
        self.assertEqual(ssids, ["strong", "weak"])
        scan.assert_called_once_with("wlan1")

    def test_scan_defaults_to_wlan0(self):
        with patch('lager.protocols.wifi.scan_wifi',
                   return_value={"access_points": []}) as scan:
            r = self._post({"action": "scan"})
        self.assertEqual(r.status_code, 200)
        scan.assert_called_once_with("wlan0")

    def test_scan_error_is_502(self):
        with patch('lager.protocols.wifi.scan_wifi',
                   return_value={"error": "no such interface"}):
            r = self._post({"action": "scan"})
        self.assertEqual(r.status_code, 502)

    def test_connect(self):
        result = {"success": True, "message": "Successfully connected to labnet",
                  "method": "nmcli"}
        with patch('lager.protocols.wifi.connect_to_wifi',
                   return_value=result) as connect:
            r = self._post({"action": "connect",
                            "params": {"ssid": "labnet",
                                       "password": "hunter22"}})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(data["value"]["connected"])
        self.assertEqual(data["value"]["method"], "nmcli")
        connect.assert_called_once_with("labnet", "hunter22", "wlan0")

    def test_connect_requires_ssid(self):
        r = self._post({"action": "connect", "params": {"password": "x"}})
        self.assertEqual(r.status_code, 400)

    def test_connect_failure_is_502(self):
        with patch('lager.protocols.wifi.connect_to_wifi',
                   return_value={"success": False, "error": "bad password"}):
            r = self._post({"action": "connect",
                            "params": {"ssid": "labnet", "password": "wrong"}})
        self.assertEqual(r.status_code, 502)
        self.assertIn("bad password", r.get_json()["error"])

    def test_delete_accepts_ssid_or_connection_name(self):
        with patch('lager.protocols.wifi.delete_wifi_connection',
                   return_value={"deleted": True}) as delete:
            r = self._post({"action": "delete", "params": {"ssid": "labnet"}})
        self.assertEqual(r.status_code, 200)
        delete.assert_called_once_with("labnet")

        with patch('lager.protocols.wifi.delete_wifi_connection',
                   return_value={"deleted": True}) as delete:
            r = self._post({"action": "delete",
                            "params": {"connection_name": "profile1"}})
        self.assertEqual(r.status_code, 200)
        delete.assert_called_once_with("profile1")

    def test_delete_requires_name(self):
        r = self._post({"action": "delete", "params": {}})
        self.assertEqual(r.status_code, 400)

    def test_delete_failure_is_502(self):
        with patch('lager.protocols.wifi.delete_wifi_connection',
                   return_value={"deleted": False, "error": "no such profile"}):
            r = self._post({"action": "delete", "params": {"ssid": "nope"}})
        self.assertEqual(r.status_code, 502)

    def test_unknown_action_is_400(self):
        r = self._post({"action": "bogus"})
        self.assertEqual(r.status_code, 400)


# ---------------------------------------------------------------------------
# /blufi/command
# ---------------------------------------------------------------------------

def _make_blufi_client(wifi_state=None, version="1.0", connect_ok=True,
                       ssid_list=None):
    client = MagicMock()
    client.connectByName.return_value = connect_ok
    client.getVersion.return_value = version
    client.getWifiState.return_value = wifi_state or {
        "opMode": 1, "staConn": 0, "softAPConn": 0}
    client.getSSIDList.return_value = ssid_list or []
    client._bleak_loop = None  # skip the loop-stop branch in _teardown
    return client


class TestBlufiHandler(unittest.TestCase):

    def setUp(self):
        app = Flask(__name__)
        blufi_handler.register_blufi_routes(app)
        self.client = app.test_client()
        # The handler sleeps between BluFi request/response pairs; stub the
        # module's time so tests don't.
        self._time_patch = patch.object(blufi_handler, 'time', MagicMock())
        self._time_patch.start()
        self.addCleanup(self._time_patch.stop)

    def _post(self, body, blufi_client=None):
        """POST with lager.blufi replaced by a fake module."""
        import lager as lager_pkg

        fake = types.ModuleType('lager.blufi')
        fake.BlufiClient = MagicMock(return_value=blufi_client or
                                     _make_blufi_client())
        fake.OP_MODE_STA = 0x01
        fake.STA_CONN_SUCCESS = 0x00
        with patch.dict(sys.modules, {'lager.blufi': fake}), \
                patch.object(lager_pkg, 'blufi', fake, create=True):
            return self.client.post('/blufi/command', json=body)

    def test_scan_keeps_blufi_uuid_or_name_match_only(self):
        devices = [
            {"name": "BLUFI_DEVICE", "address": "AA:BB:CC:DD:EE:01",
             "rssi": -60, "uuids": [blufi_handler.BLUFI_SERVICE_UUID]},
            {"name": "esp-target", "address": "AA:BB:CC:DD:EE:02",
             "rssi": -70, "uuids": []},
            {"name": "headphones", "address": "AA:BB:CC:DD:EE:03",
             "rssi": -50, "uuids": ["0000180f-0000-1000-8000-00805f9b34fb"]},
        ]
        with patch.object(blufi_handler, 'run_bleak',
                          _fake_run_bleak(devices)):
            r = self._post({"action": "scan", "params": {"name_contains": "esp"}})
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        names = [d["name"] for d in data["value"]["devices"]]
        self.assertEqual(sorted(names), ["BLUFI_DEVICE", "esp-target"])

    def test_connect_reports_version_and_state(self):
        client = _make_blufi_client()
        r = self._post({"action": "connect",
                        "params": {"device_name": "BLUFI_DEVICE"}}, client)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        value = data["value"]
        self.assertEqual(value["version"], "1.0")
        self.assertEqual(value["opModeName"], "STA")
        self.assertEqual(value["staConnName"], "Connected")
        client.connectByName.assert_called_once_with(
            "BLUFI_DEVICE", timeout=20.0)
        client.negotiateSecurity.assert_called_once_with()
        # The per-request client is fully torn down (long-lived server).
        client._cleanup.assert_called()

    def test_connect_requires_device_name(self):
        r = self._post({"action": "connect", "params": {}})
        self.assertEqual(r.status_code, 400)

    def test_connect_failure_is_502_and_cleans_up(self):
        client = _make_blufi_client(connect_ok=False)
        r = self._post({"action": "connect",
                        "params": {"device_name": "GHOST"}}, client)
        self.assertEqual(r.status_code, 502)
        client._cleanup.assert_called()

    def test_provision_success(self):
        client = _make_blufi_client()
        r = self._post({"action": "provision",
                        "params": {"device_name": "BLUFI_DEVICE",
                                   "ssid": "labnet",
                                   "password": "hunter22"}}, client)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"]["ssid"], "labnet")
        self.assertEqual(data["value"]["staConnName"], "Connected")
        client.postDeviceMode.assert_called_once_with(0x01)
        client.postStaWifiInfo.assert_called_once_with(
            {"ssid": "labnet", "pass": "hunter22"})

    def test_provision_requires_credentials(self):
        r = self._post({"action": "provision",
                        "params": {"device_name": "BLUFI_DEVICE",
                                   "ssid": "labnet"}})
        self.assertEqual(r.status_code, 400)

    def test_provision_target_not_connected_is_502(self):
        client = _make_blufi_client(
            wifi_state={"opMode": 1, "staConn": 1, "softAPConn": 0})
        r = self._post({"action": "provision",
                        "params": {"device_name": "BLUFI_DEVICE",
                                   "ssid": "labnet",
                                   "password": "wrong"}}, client)
        self.assertEqual(r.status_code, 502)
        self.assertIn("Failed", r.get_json()["error"])
        client._cleanup.assert_called()

    def test_wifi_scan_sorts_by_rssi(self):
        client = _make_blufi_client(ssid_list=[
            {"ssid": "weak", "rssi": -80}, {"ssid": "strong", "rssi": -40}])
        r = self._post({"action": "wifi_scan",
                        "params": {"device_name": "BLUFI_DEVICE",
                                   "scan_timeout": 5}}, client)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        ssids = [n["ssid"] for n in data["value"]["networks"]]
        self.assertEqual(ssids, ["strong", "weak"])
        client.requestDeviceScan.assert_called_once_with(timeout=5.0)

    def test_status(self):
        client = _make_blufi_client(
            wifi_state={"opMode": 2, "staConn": 3, "softAPConn": 1})
        r = self._post({"action": "status",
                        "params": {"device_name": "BLUFI_DEVICE"}}, client)
        value = r.get_json()["value"]
        self.assertEqual(r.status_code, 200)
        self.assertEqual(value["opModeName"], "SoftAP")
        self.assertEqual(value["staConnName"], "No IP")
        self.assertEqual(value["softAPConn"], 1)

    def test_version(self):
        client = _make_blufi_client(version="2.3")
        r = self._post({"action": "version",
                        "params": {"device_name": "BLUFI_DEVICE"}}, client)
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["value"]["version"], "2.3")
        self.assertIn("2.3", data["message"])

    def test_unknown_action_is_400(self):
        r = self._post({"action": "bogus"})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
