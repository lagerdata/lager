# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the ``hsm`` role of POST /net/command
(box/lager/http_handlers/net_command.py).

The handler maps each action to one hsm_hs method on a hardware_service
Device proxy. These tests mock ``net_command.Device`` and ``Net`` (as
test_net_command_handler.py does) and assert the proxy call, the login
kwargs, the request validation and the response shape.
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

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from flask import Flask  # noqa: E402
from lager.http_handlers import net_command  # noqa: E402
from lager.nets.device import DeviceError  # noqa: E402

_PASSWORD = 'correct-horse-battery'

SAVED_NETS = [
    {"name": "hsm1", "role": "hsm", "instrument": "YubiHSM_2",
     "address": "USB0::0x1050::0x0030::12345678::INSTR"},
]


class TestHsmNetCommand(unittest.TestCase):

    def setUp(self):
        app = Flask(__name__)
        net_command.register_net_command_routes(app)
        self.client = app.test_client()

    def _run(self, action, params=None, dev=None):
        dev = MagicMock() if dev is None else dev
        body = {'netname': 'hsm1', 'action': action, 'params': params or {}}
        with patch('lager.http_handlers.net_command.Net') as NetMock, \
                patch('lager.http_handlers.net_command.Device',
                      return_value=dev) as DeviceMock:
            NetMock.get_local_nets.return_value = SAVED_NETS
            resp = self.client.post('/net/command', json=body)
        self.DeviceMock = DeviceMock
        return resp, dev

    def _auth(self, **params):
        return {'password': _PASSWORD, **params}

    # ----- routing -----

    def test_routes_to_hsm_hs_with_serial_device_id(self):
        dev = MagicMock()
        dev.info.return_value = {'firmware': '2.4.0', 'serial': 12345678,
                                 'log_used': 3, 'log_size': 62}
        resp, _ = self._run('info', dev=dev)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        args, kwargs = self.DeviceMock.call_args
        self.assertEqual(args[0], 'hsm_hs')
        self.assertEqual(args[1]['name'], 'hsm1')
        self.assertEqual(args[1]['device_id'], 'yubihsm:12345678')

    def test_info_needs_no_password(self):
        dev = MagicMock()
        dev.info.return_value = {'firmware': '2.4.0', 'serial': 12345678,
                                 'log_used': 3, 'log_size': 62}
        resp, _ = self._run('info', dev=dev)
        body = resp.get_json()
        self.assertEqual(body['message'], 'YubiHSM fw 2.4.0, serial 12345678, log 3/62')
        self.assertEqual(body['value']['serial'], 12345678)

    def test_unknown_action(self):
        resp, _ = self._run('explode', self._auth())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unknown action 'explode'", resp.get_json()['error'])

    # ----- login -----

    def test_missing_password_is_400(self):
        for action in ('blink', 'objects', 'pubkey', 'sign', 'reset'):
            resp, dev = self._run(action, {'object_id': 1})
            self.assertEqual(resp.status_code, 400, action)
            self.assertIn('password', resp.get_json()['error'])
            self.assertEqual(dev.method_calls, [], action)

    def test_empty_password_is_400(self):
        resp, dev = self._run('objects', {'password': ''})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(dev.method_calls, [])

    def test_password_never_in_response(self):
        dev = MagicMock()
        dev.list_objects.return_value = []
        resp, _ = self._run('objects', self._auth(auth_key_id=2), dev=dev)
        self.assertNotIn(_PASSWORD, resp.get_data(as_text=True))
        dev.list_objects.assert_called_once_with(
            type=None, label=None, auth_key_id=2, password=_PASSWORD)

    # ----- actions -----

    def test_blink_widens_proxy_timeout(self):
        resp, dev = self._run('blink', self._auth(seconds=20))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        dev.blink.assert_called_once_with(20, auth_key_id=1, password=_PASSWORD)
        self.assertEqual(self.DeviceMock.call_args.kwargs['timeout'], 35.0)

    def test_objects_validates_type(self):
        resp, dev = self._run('objects', self._auth(type='banana'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Unknown object type', resp.get_json()['error'])
        dev.list_objects.assert_not_called()

    def test_objects_message(self):
        dev = MagicMock()
        dev.list_objects.return_value = [{'id': 1}, {'id': 2}]
        resp, _ = self._run('objects', self._auth(type='asymmetric-key', label='x'), dev=dev)
        self.assertEqual(resp.get_json()['message'], '2 object(s)')
        self.assertEqual(dev.list_objects.call_args.kwargs['type'], 'asymmetric-key')

    def test_delete_defaults_to_asymmetric_key(self):
        resp, dev = self._run('delete', self._auth(object_id=7))
        self.assertEqual(resp.get_json()['message'], 'Deleted asymmetric-key 0x0007')
        dev.delete_object.assert_called_once_with(
            7, 'asymmetric-key', auth_key_id=1, password=_PASSWORD)

    def test_generate(self):
        dev = MagicMock()
        dev.generate_key.return_value = {'id': 0x42, 'public_key_pem': 'PEM'}
        resp, _ = self._run('generate', self._auth(algorithm='ec-p256', label='k',
                                                    domains=[1, 2]), dev=dev)
        body = resp.get_json()
        self.assertEqual(body['message'], 'Generated ec-p256 key 0x0042')
        self.assertEqual(body['value']['public_key_pem'], 'PEM')
        dev.generate_key.assert_called_once_with(
            0, 'ec-p256', 'k', [1, 2], None, auth_key_id=1, password=_PASSWORD)

    def test_generate_requires_known_algorithm(self):
        resp, dev = self._run('generate', self._auth(algorithm='dsa'))
        self.assertEqual(resp.status_code, 400)
        dev.generate_key.assert_not_called()
        resp, _ = self._run('generate', self._auth())
        self.assertEqual(resp.status_code, 400)
        self.assertIn('algorithm', resp.get_json()['error'])

    def test_generate_refuses_rsa(self):
        resp, dev = self._run('generate', self._auth(algorithm='rsa2048'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('lager python', resp.get_json()['error'])
        dev.generate_key.assert_not_called()

    def test_pubkey(self):
        dev = MagicMock()
        dev.get_public_key.return_value = 'PEM'
        resp, _ = self._run('pubkey', self._auth(object_id=7), dev=dev)
        self.assertEqual(resp.get_json()['value'], 'PEM')

    def test_sign_returns_base64_signature(self):
        dev = MagicMock()
        dev.sign.return_value = 'c2ln'
        resp, _ = self._run('sign', self._auth(object_id=7, data='bXNn',
                                                mechanism='ecdsa-sha256'), dev=dev)
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        self.assertEqual(body['value'], {'signature': 'c2ln'})
        dev.sign.assert_called_once_with(7, 'bXNn', 'ecdsa-sha256',
                                         auth_key_id=1, password=_PASSWORD)

    def test_sign_validates_mechanism(self):
        resp, dev = self._run('sign', self._auth(object_id=7, data='bXNn', mechanism='md5'))
        self.assertEqual(resp.status_code, 400)
        dev.sign.assert_not_called()

    def test_reset(self):
        resp, dev = self._run('reset', self._auth())
        self.assertEqual(resp.status_code, 200, resp.get_json())
        dev.reset.assert_called_once_with(auth_key_id=1, password=_PASSWORD)
        self.assertEqual(self.DeviceMock.call_args.kwargs['timeout'], 60.0)

    # ----- errors -----

    def test_hardware_error_drops_wrapper_text(self):
        dev = MagicMock()
        dev.get_public_key.side_effect = DeviceError(
            'Function call failed: [HSM] No asymmetric-key with ID 0x0009')
        resp, _ = self._run('pubkey', self._auth(object_id=9), dev=dev)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json()['error'],
                         'Hardware error: [HSM] No asymmetric-key with ID 0x0009')


if __name__ == '__main__':
    unittest.main()
