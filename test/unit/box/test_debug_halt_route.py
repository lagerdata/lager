# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for `POST /debug/halt` on the box debug service.

`DebugNet.halt()` -- OpenOCD's bare ``halt``, which stops the core without
pulsing nRESET -- existed only in the on-box Python API, which drives the
OpenOCD TCL RPC directly. No HTTP client could reach it, so the Rust crate
had no way to halt a part that executes in place. This pins the route: the
OpenOCD path sends ``halt`` (never ``reset halt``), J-Link answers 400 with
the halt-first .JLinkScript pointer the Python API gives, and the route runs
under the probe lock like `/debug/reset`.

service.py pulls in the hardware driver stack transitively, so the same
module stubs the rest of test/unit/box/ uses are installed here too --
`setdefault`, never a meta_path hook.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    mod.__path__ = []
    return mod


def _stub(dotted):
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


for _dep in ['pyvisa', 'pyvisa.constants', 'usb', 'usb.util', 'usb.core', 'pigpio',
             'labjack', 'labjack.ljm', 'nidaqmx', 'bleak', 'serial',
             'serial.tools', 'serial.tools.list_ports',
             'pygdbmi', 'pygdbmi.gdbcontroller', 'pygdbmi.constants']:
    _stub(_dep)

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')))

from lager.debug import service  # noqa: E402


class _Recorder:
    """Stands in for the BaseHTTPRequestHandler response writers."""

    def __init__(self):
        self.status = None
        self.payload = None

    def send_json_response(self, status_code, data):
        self.status = status_code
        self.payload = data


def _handler():
    """A DebugServiceHandler that records responses instead of writing them."""
    handler = service.DebugServiceHandler.__new__(service.DebugServiceHandler)
    recorder = _Recorder()
    handler.send_json_response = recorder.send_json_response
    handler.send_error_response = lambda code, message: (
        recorder.send_json_response(code, {'error': message, 'status': 'error'}))
    return handler, recorder


class _FakeRpc:
    """Records the commands the handler sends over the OpenOCD TCL RPC."""

    calls = []
    output = 'target halted due to debug-request, current mode: Thread'
    error = None

    def __init__(self, port):
        _FakeRpc.calls.append(('port', port))

    def halt(self):
        _FakeRpc.calls.append(('halt',))
        if _FakeRpc.error is not None:
            raise _FakeRpc.error
        return _FakeRpc.output

    def reset(self, halt=False):
        _FakeRpc.calls.append(('reset', halt))
        return ''


def _halt(backend=None, running=True, rpc_error=None, output=None):
    handler, recorder = _handler()
    _FakeRpc.calls = []
    _FakeRpc.error = rpc_error
    _FakeRpc.output = output if output is not None else _FakeRpc.output
    with patch.object(service, 'resolve_backend',
                      lambda net: backend or service.BACKEND_OPENOCD), \
         patch.object(service, '_resolve_probe',
                      lambda net: ('FT4232H-1', 1, 3333, 3334, 3335, 3336)), \
         patch.object(service, '_openocd_ports_for_slot', lambda slot: (4445, 6667)), \
         patch.object(service, 'get_openocd_status',
                      lambda serial=None: {'running': running}), \
         patch.object(service, 'OpenOcdRpc', _FakeRpc):
        handler.handle_halt({'net': {'name': 'debug1', 'role': 'debug'}})
    return recorder


class HaltRouteTests(unittest.TestCase):

    def setUp(self):
        self._output = _FakeRpc.output

    def tearDown(self):
        _FakeRpc.output = self._output
        _FakeRpc.error = None

    def test_openocd_sends_a_bare_halt_on_the_slots_tcl_port(self):
        recorder = _halt()
        self.assertEqual(recorder.status, 200)
        self.assertEqual(_FakeRpc.calls, [('port', 6667), ('halt',)])
        self.assertEqual(recorder.payload['status'], 'halt_complete')
        self.assertEqual(recorder.payload['backend'], service.BACKEND_OPENOCD)
        self.assertEqual(recorder.payload['output'], [_FakeRpc.output])

    def test_halt_never_resets(self):
        # The point of the route: reset(halt=True) pulses nRESET.
        _halt()
        self.assertNotIn('reset', [c[0] for c in _FakeRpc.calls])

    def test_empty_rpc_output_is_an_empty_list(self):
        recorder = _halt(output='')
        self.assertEqual(recorder.status, 200)
        self.assertEqual(recorder.payload['output'], [])

    def test_jlink_is_refused_with_the_script_pointer(self):
        recorder = _halt(backend=service.BACKEND_JLINK)
        self.assertEqual(recorder.status, 400)
        self.assertIn('OpenOCD-only', recorder.payload['error'])
        self.assertIn('.JLinkScript', recorder.payload['error'])
        self.assertIn("'debug1'", recorder.payload['error'])
        self.assertEqual(_FakeRpc.calls, [])

    def test_no_running_openocd_is_400(self):
        recorder = _halt(running=False)
        self.assertEqual(recorder.status, 400)
        self.assertEqual(recorder.payload['error'], 'No debugger connection found')
        self.assertEqual(_FakeRpc.calls, [])

    def test_an_rpc_error_is_500_with_its_message(self):
        recorder = _halt(rpc_error=service.OpenOcdRpcError('halt: target not examined'))
        self.assertEqual(recorder.status, 500)
        self.assertIn('target not examined', recorder.payload['error'])


class HaltRoutingTests(unittest.TestCase):

    def test_halt_runs_under_the_probe_lock(self):
        self.assertEqual(service._PROBE_LOCKED_ENDPOINTS['/debug/halt'], 'halt')
        self.assertTrue(callable(getattr(service.DebugServiceHandler, 'handle_halt')))

    def test_health_advertises_halt(self):
        # The Rust crate gates DebugNet::halt() on this, not on a version.
        self.assertIn('halt', service.SERVICE_FEATURES)
        self.assertIn('erase_range', service.SERVICE_FEATURES)


if __name__ == '__main__':
    unittest.main()
