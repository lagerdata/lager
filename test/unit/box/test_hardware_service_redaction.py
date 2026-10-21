# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for kwarg redaction in the ``/invoke`` log line of
box/lager/hardware_service.py.

``/invoke`` logs every call with its arguments to a world-readable log file.
Drivers that log in to a device (the HSM net) receive the password as a
keyword argument, so the log line must replace secret keyword values while
the driver still receives the real value.
"""

import logging
import os
import sys
import types
import unittest
from unittest.mock import MagicMock


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted):
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
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

import lager.hardware_service as hw  # noqa: E402

_ADDRESS = 'USB0::0x1050::0x0030::12345678::INSTR'
_SECRET = 'hunter2-not-for-logs'


class _RecordingDevice:
    def __init__(self):
        self.kwargs = None

    def sign(self, *args, **kwargs):
        self.kwargs = kwargs
        return 'ok'


class RedactKwargsTests(unittest.TestCase):

    def test_secret_keys_are_masked(self):
        out = hw._redact_kwargs({'password': _SECRET, 'pin': '1234',
                                 'private_key_pem': 'PEM', 'object_id': 7})
        self.assertEqual(out, {'password': '***', 'pin': '***',
                               'private_key_pem': '***', 'object_id': 7})

    def test_input_is_not_mutated(self):
        kwargs = {'password': _SECRET}
        hw._redact_kwargs(kwargs)
        self.assertEqual(kwargs, {'password': _SECRET})

    def test_non_dict_passes_through(self):
        self.assertIsNone(hw._redact_kwargs(None))
        self.assertEqual(hw._redact_kwargs([1, 2]), [1, 2])


class InvokeLogRedactionTests(unittest.TestCase):

    def setUp(self):
        hw.device_cache.clear()
        hw.module_cache.clear()
        with hw.device_locks_meta_lock:
            hw.device_locks.clear()
        self.client = hw.app.test_client()

    def test_password_reaches_driver_but_not_log(self):
        device = _RecordingDevice()
        hw.device_cache[('hsm_hs', _ADDRESS)] = device
        payload = {'device': 'hsm_hs', 'function': 'sign', 'args': [1],
                   'kwargs': {'auth_key_id': 1, 'password': _SECRET},
                   'net_info': {'address': _ADDRESS, 'channel': 0}}

        with self.assertLogs(hw.logger, level=logging.INFO) as captured:
            resp = self.client.post('/invoke', json=payload)

        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(device.kwargs, {'auth_key_id': 1, 'password': _SECRET})
        joined = '\n'.join(captured.output)
        self.assertIn('Invoking hsm_hs.sign', joined)
        self.assertIn("'password': '***'", joined)
        self.assertNotIn(_SECRET, joined)


if __name__ == '__main__':
    unittest.main()
