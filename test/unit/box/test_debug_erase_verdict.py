# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the erase verdict on the box's J-Link path.

`/debug/erase` answered HTTP 200 with ``status: erase_complete`` whenever
``chip_erase()`` raised nothing. But J-Link Commander does not raise when it
cannot attach -- it prints and carries on -- and ``chip_erase()`` is a
generator that only yields its stdout, with no success channel at all. So with
the probe enumerated and the target unplugged, the box reported a completed
erase, the CLI printed "Erase complete!", and the part was never touched.

The OpenOCD branches of the same handler were already strict: ``erase_range()``
raises ``Da1469xLoaderError`` and ``rpc.flash_erase_all()`` raises
``OpenOcdRpcError``, both surfacing as 500. This pins the J-Link equivalent.

Also pins the predicate split. ``_connect_failed`` drives the flash-path
*retry*, where a false positive costs one extra attempt. ``_attach_failed`` is
the *verdict*, and drops ``could not read cpuid`` -- a line J-Link emits per
access port during a scan, which does not on its own mean the session never
attached.

service.py pulls in the hardware driver stack transitively, so the same
module stubs the rest of test/unit/box/ uses are installed here too --
`setdefault`, never a meta_path hook, so a real dependency that IS installed
keeps winning and nothing leaks into the tests that run after this one.
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


# `pygdbmi.gdbcontroller` / `pygdbmi.constants` are the two this module needs
# beyond the shared list: lager.debug.api imports lager.debug.gdb, which
# imports GdbController at module scope.
for _dep in ['pyvisa', 'pyvisa.constants', 'usb', 'usb.util', 'usb.core', 'pigpio',
             'labjack', 'labjack.ljm', 'nidaqmx', 'bleak', 'serial',
             'serial.tools', 'serial.tools.list_ports',
             'pygdbmi', 'pygdbmi.gdbcontroller', 'pygdbmi.constants']:
    _stub(_dep)

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')))

from lager.debug import api, service  # noqa: E402


# Captured shapes. The failure is the one this issue is about: probe
# enumerated, target unreachable over SWD.
ERASE_CONNECT_FAILED = [
    'Connecting to J-Link...',
    'Target voltage: 0.00 V',
    'Connecting to target...',
    'AP[0]: Skipped. Could not read CPUID register',
    'Attach to CPU failed. Executing connect under reset.',
    'Failed to power up DAP',
    'ERROR: Could not connect to target.',
]

ERASE_OK = [
    'Cortex-M33 identified.',
    'Erasing device...',
    'Erasing done.',
    'O.K.',
]


class _Recorder:
    """Stands in for the BaseHTTPRequestHandler response writers."""

    def __init__(self):
        self.status = None
        self.payload = None

    def send_json_response(self, status_code, data):
        self.status = status_code
        self.payload = data


def _handler():
    """A DebugServiceHandler that records responses instead of writing them.

    Built with __new__: BaseHTTPRequestHandler.__init__ would try to service a
    socket.
    """
    handler = service.DebugServiceHandler.__new__(service.DebugServiceHandler)
    recorder = _Recorder()
    handler.send_json_response = recorder.send_json_response
    handler.send_error_response = lambda code, message: (
        recorder.send_json_response(code, {'error': message, 'status': 'error'}))
    return handler, recorder


def _erase(output):
    """Drive handle_erase down the J-Link path with `output` as JLinkExe's."""
    handler, recorder = _handler()
    with patch.object(service.safety, 'check_destructive', lambda net, op: None), \
         patch.object(service, '_resolve_device_type',
                      lambda net: 'NRF5340_XXAA_APP'), \
         patch.object(service, 'resolve_backend',
                      lambda net: service.BACKEND_JLINK), \
         patch.object(service, '_resolve_probe',
                      lambda net: ('000051014439', 0, 2331, 2332, 2333, 2334)), \
         patch.object(service, '_openocd_ports_for_slot', lambda slot: (4444, 6666)), \
         patch.object(service, '_get_script_file', lambda net: None), \
         patch.object(service, 'chip_erase', lambda **kwargs: iter(output)):
        handler.handle_erase({'net': {'name': 'debug1', 'role': 'debug'}})
    return recorder


class EraseVerdictTests(unittest.TestCase):
    """The box must not answer 200 for an erase that never attached."""

    def test_unreachable_target_is_a_500(self):
        recorder = _erase(ERASE_CONNECT_FAILED)
        self.assertEqual(recorder.status, 500)
        self.assertEqual(recorder.payload['status'], 'error')

    def test_the_500_carries_the_programmer_output(self):
        # The CLI surfaces response.json()['error'], so the operator's only
        # view of what the probe actually said is this string.
        recorder = _erase(ERASE_CONNECT_FAILED)
        self.assertIn('ERROR: Could not connect to target.',
                      recorder.payload['error'])
        self.assertIn('nothing was erased', recorder.payload['error'])

    def test_a_completed_erase_is_still_a_200(self):
        recorder = _erase(ERASE_OK)
        self.assertEqual(recorder.status, 200)
        self.assertEqual(recorder.payload['status'], 'erase_complete')
        self.assertEqual(recorder.payload['backend'], service.BACKEND_JLINK)

    def test_silent_output_keeps_its_existing_meaning(self):
        # An older JLinkExe, or one that printed nothing. Unrecognised output
        # must not be newly reported as failing.
        recorder = _erase([])
        self.assertEqual(recorder.status, 200)
        self.assertEqual(recorder.payload['output'], 'Erase completed')


class AttachFailedIsStricterThanConnectFailed(unittest.TestCase):
    """The retry predicate and the verdict predicate differ by exactly one
    line, and that difference is the point."""

    CPUID_ONLY = ['AP[0]: Skipped. Could not read CPUID register']

    def test_retry_predicate_still_fires_on_a_skipped_ap(self):
        # Unchanged behaviour on the flash retry path.
        self.assertTrue(api._connect_failed(self.CPUID_ONLY))

    def test_verdict_predicate_does_not(self):
        self.assertFalse(api._attach_failed(self.CPUID_ONLY))

    def test_both_fire_on_the_real_failure(self):
        self.assertTrue(api._connect_failed(ERASE_CONNECT_FAILED))
        self.assertTrue(api._attach_failed(ERASE_CONNECT_FAILED))

    def test_each_verdict_variant_alone_is_enough(self):
        for line in ('ERROR: Could not connect to target.',
                     'Could not connect to the target device.',
                     'Cannot connect to target.',
                     'Failed to power up DAP'):
            with self.subTest(line=line):
                self.assertTrue(api._attach_failed([line]))

    def test_a_clean_session_is_not_a_failure(self):
        self.assertFalse(api._attach_failed(ERASE_OK))
        self.assertFalse(api._attach_failed([]))


if __name__ == '__main__':
    unittest.main()
