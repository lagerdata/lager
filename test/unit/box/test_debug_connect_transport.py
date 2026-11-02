# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""``transport`` on POST /debug/connect reaches the backend that starts.

The handler used to start both backends with SWD whatever the request said,
while ``DebugNet.connect(transport=...)`` passed the caller's choice through.
These tests run the real handler with the backends replaced by recorders.

Hardware-only deps are stubbed the way test_debug_connect_ports.py does it.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


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


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core', 'pigpio',
    'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
    'pexpect', 'pexpect.replwrap', 'pexpect.exceptions',
    'pygdbmi', 'pygdbmi.gdbcontroller', 'pygdbmi.constants',
]:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.debug import service  # noqa: E402
from lager.debug.probes import BACKEND_JLINK, BACKEND_OPENOCD  # noqa: E402


class TransportOrReject(unittest.TestCase):
    def test_absent_means_swd(self):
        self.assertEqual(service._transport_or_reject({}), 'SWD')
        self.assertEqual(service._transport_or_reject({'transport': None}), 'SWD')

    def test_names_are_case_insensitive(self):
        for raw, want in (('swd', 'SWD'), ('JTAG', 'JTAG'), (' jtag ', 'JTAG')):
            with self.subTest(raw=raw):
                self.assertEqual(service._transport_or_reject({'transport': raw}), want)

    def test_anything_else_is_a_bad_request(self):
        for bad in ('cJTAG', 'swd -nogui', '', 1, True, ['SWD']):
            with self.subTest(value=bad):
                with self.assertRaises(service.BadDebugRequest) as ctx:
                    service._transport_or_reject({'transport': bad})
                self.assertIn('transport must be one of SWD, JTAG', str(ctx.exception))

    def test_health_lists_the_feature(self):
        self.assertIn('connect_transport', service.SERVICE_FEATURES)


class ConnectTransport(unittest.TestCase):
    """The real handle_connect, with the backends replaced by recorders."""

    NET = {'name': 'SWD1', 'device': 'nrf52840_xxaa'}

    def setUp(self):
        self.responses = []
        self.started = []
        self.stopped = []
        self.running = {'running': False, 'pid': None}
        service.active_connections.clear()
        self.addCleanup(service.active_connections.clear)

    def _start(self, backend):
        def start(**kwargs):
            self.started.append((backend, kwargs))
            return {'pid': 4242, 'telnet_port': 4444}
        return start

    def _connect(self, backend, body):
        handler = service.DebugServiceHandler.__new__(service.DebugServiceHandler)
        handler.send_json_response = lambda code, data: self.responses.append((code, data))
        handler.send_error_response = lambda code, message: self.responses.append(
            (code, {'error': message}))
        handler._get_jlink_script_from_net = lambda net: None
        patches = [
            patch.object(service, 'resolve_backend', return_value=backend),
            patch.object(service, '_resolve_device_type', return_value='nrf52840_xxaa'),
            patch.object(service, '_resolve_probe',
                         return_value=('123', 0, 2331, 2332, 2333, 9090)),
            patch.object(service, '_get_openocd_config_file', return_value=None),
            patch.object(service, 'get_jlink_gdbserver_status', return_value=self.running),
            patch.object(service, 'get_openocd_status', return_value=self.running),
            patch.object(service, 'stop_jlink_gdbserver',
                         side_effect=lambda **k: self.stopped.append(BACKEND_JLINK)),
            patch.object(service, 'stop_openocd',
                         side_effect=lambda **k: self.stopped.append(BACKEND_OPENOCD)),
            patch.object(service, 'start_jlink_gdbserver', side_effect=self._start(BACKEND_JLINK)),
            patch.object(service, 'start_openocd_gdbserver',
                         side_effect=self._start(BACKEND_OPENOCD)),
            patch.object(service.time, 'sleep'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        handler.handle_connect({'net': dict(self.NET), **body})
        return self.responses[-1]

    def test_jlink_starts_with_the_requested_transport(self):
        code, data = self._connect(BACKEND_JLINK, {'transport': 'jtag'})
        self.assertEqual(code, 200)
        self.assertEqual(self.started[-1][0], BACKEND_JLINK)
        self.assertEqual(self.started[-1][1]['transport'], 'JTAG')
        self.assertEqual(data['transport'], 'JTAG')

    def test_openocd_starts_with_the_requested_transport(self):
        code, data = self._connect(BACKEND_OPENOCD, {'transport': 'JTAG'})
        self.assertEqual(code, 200)
        self.assertEqual(self.started[-1][0], BACKEND_OPENOCD)
        self.assertEqual(self.started[-1][1]['transport'], 'JTAG')

    def test_no_transport_still_starts_swd(self):
        code, data = self._connect(BACKEND_JLINK, {})
        self.assertEqual(code, 200)
        self.assertEqual(self.started[-1][1]['transport'], 'SWD')
        self.assertEqual(data['transport'], 'SWD')

    def test_a_bad_transport_is_400_and_touches_nothing(self):
        code, data = self._connect(BACKEND_JLINK, {'transport': 'cJTAG'})
        self.assertEqual(code, 400)
        self.assertIn('transport must be one of', data['error'])
        self.assertEqual(self.started, [])
        self.assertEqual(self.stopped, [])

    def test_a_running_server_on_the_same_transport_is_reused(self):
        self._connect(BACKEND_JLINK, {'transport': 'JTAG'})
        self.running.update(running=True, pid=4242)
        self.started.clear()
        code, data = self._connect(BACKEND_JLINK, {'transport': 'jtag'})
        self.assertEqual(code, 200)
        self.assertEqual(data['gdb_server']['status'], 'already_running')
        self.assertEqual(self.started, [])

    def test_a_running_server_on_another_transport_is_restarted(self):
        self._connect(BACKEND_JLINK, {})
        self.running.update(running=True, pid=4242)
        self.started.clear()
        self.stopped.clear()
        code, data = self._connect(BACKEND_JLINK, {'transport': 'JTAG'})
        self.assertEqual(code, 200)
        self.assertEqual(data['gdb_server']['status'], 'started')
        self.assertIn(BACKEND_JLINK, self.stopped)
        self.assertEqual(self.started[-1][1]['transport'], 'JTAG')

    def test_a_server_tracked_before_transport_counts_as_swd(self):
        # An entry written by a service that predates the field has no
        # 'transport' key; the server it describes was started with SWD.
        service.active_connections['SWD1:nrf52840_xxaa'] = {
            'device': 'nrf52840_xxaa', 'backend': BACKEND_JLINK, 'pid': 4242}
        self.running.update(running=True, pid=4242)
        code, data = self._connect(BACKEND_JLINK, {})
        self.assertEqual(code, 200)
        self.assertEqual(data['gdb_server']['status'], 'already_running')


if __name__ == '__main__':
    unittest.main()
