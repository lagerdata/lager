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
import tempfile
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


def _erase(output, request=None, device='NRF5340_XXAA_APP', seen=None,
           script=None, during_erase=None):
    """Drive handle_erase down the J-Link path with `output` as JLinkExe's.

    `request` adds keys to the POST body (`erase_start` / `erase_size`);
    `seen`, a dict, receives the kwargs `chip_erase` was called with;
    `script` is what `_get_script_file` hands back; `during_erase` runs
    inside the fake `chip_erase`, standing in for whatever another session
    does to the box while Commander is busy.
    """
    handler, recorder = _handler()

    def chip_erase(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        if during_erase is not None:
            during_erase()
        return iter(output)

    with patch.object(service.safety, 'check_destructive', lambda net, op: None), \
         patch.object(service, '_resolve_device_type', lambda net: device), \
         patch.object(service, 'resolve_backend',
                      lambda net: service.BACKEND_JLINK), \
         patch.object(service, '_resolve_probe',
                      lambda net: ('000051014439', 0, 2331, 2332, 2333, 2334)), \
         patch.object(service, '_openocd_ports_for_slot', lambda slot: (4444, 6666)), \
         patch.object(service, '_get_script_file', lambda net: script), \
         patch.object(service, 'chip_erase', chip_erase):
        handler.handle_erase({'net': {'name': 'debug1', 'role': 'debug'}, **(request or {})})
    return recorder


class EraseReportTests(unittest.TestCase):
    """The `erase_range` a 200 reports is the range the erase was given, read
    before Commander ran, not whatever the per-net script says afterwards.

    On a shared bench, a `disconnect` on the same net cleared the script
    between the erase and the report: the erase ran the script's 1536 KiB and
    the JSON said `default`, 1 MiB. Found on a DA1469x during the hardware
    validation of the feature.
    """

    XIP = 0x16000000
    SCRIPT_RANGE = {
        'start': XIP, 'end': XIP + 0x17FFFF, 'length': 0x180000,
        'source': 'script', 'text': '0x16000000-0x1617FFFF (1536 KiB)',
    }

    def _script(self):
        # Under /tmp on purpose: chip_erase's containment rule wants the
        # runtime root, and macOS's default tempdir is elsewhere.
        handle = tempfile.NamedTemporaryFile(
            'w', dir=api._probes.RUNTIME_DIR, suffix='.JLinkScript', delete=False)
        handle.write('LAGER_ERASE_RANGE: 0x16000000 0x1617FFFF\n')
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        return handle.name

    def test_the_script_range_is_reported_even_when_the_script_vanishes_mid_erase(self):
        script = self._script()
        recorder = _erase(ERASE_OK, device='DA14695', script=script,
                          during_erase=lambda: os.unlink(script))
        self.assertEqual(recorder.status, 200)
        self.assertEqual(recorder.payload['erase_range'], self.SCRIPT_RANGE)

    def test_the_script_range_is_reported_when_the_script_stays(self):
        recorder = _erase(ERASE_OK, device='DA14695', script=self._script())
        self.assertEqual(recorder.payload['erase_range'], self.SCRIPT_RANGE)

    def test_a_request_still_wins_over_the_script(self):
        recorder = _erase(ERASE_OK, {'erase_start': self.XIP, 'erase_size': 0x200000},
                          device='DA14695', script=self._script())
        self.assertEqual(recorder.payload['erase_range']['source'], 'request')
        self.assertEqual(recorder.payload['erase_range']['length'], 0x200000)

    def test_the_plan_follows_chip_erase_script_rules(self):
        # The same helper chip_erase uses: a missing file is no script, a
        # path outside the runtime root is refused, a request beats a script.
        script = self._script()
        self.assertEqual(api.jlink_erase_plan('DA14695', script),
                         (self.XIP, 0x180000, 'script'))
        os.unlink(script)
        self.assertEqual(api.jlink_erase_plan('DA14695', script),
                         (self.XIP, 0x100000, 'default'))
        self.assertEqual(api.jlink_erase_plan('DA14695', None, start=self.XIP, length=0x1000),
                         (self.XIP, 0x1000, 'request'))
        self.assertIsNone(api.jlink_erase_plan('NRF5340_XXAA_APP', None))
        with self.assertRaises(ValueError):
            api.jlink_erase_plan('DA14695', '/etc/not-a-runtime-path.JLinkScript')


class EraseRangeTests(unittest.TestCase):
    """`erase_start` / `erase_size` in the body reach `chip_erase`, are
    checked before it runs, and the range erased is reported back."""

    XIP = 0x16000000

    def test_no_range_forwards_none_and_reports_a_full_chip(self):
        seen = {}
        recorder = _erase(ERASE_OK, seen=seen)
        self.assertEqual(recorder.status, 200)
        self.assertEqual((seen['start'], seen['length']), (None, None))
        self.assertIn('erase_range', recorder.payload)
        self.assertIsNone(recorder.payload['erase_range'])

    def test_a_da1469x_with_no_range_reports_the_default(self):
        recorder = _erase(ERASE_OK, device='DA14695')
        self.assertEqual(recorder.status, 200)
        self.assertEqual(recorder.payload['erase_range'], {
            'start': self.XIP, 'end': self.XIP + 0xFFFFF, 'length': 0x100000,
            'source': 'default', 'text': '0x16000000-0x160FFFFF (1 MiB)',
        })

    def test_a_request_reaches_chip_erase_and_is_reported(self):
        seen = {}
        recorder = _erase(ERASE_OK, {'erase_start': self.XIP, 'erase_size': 0x200000},
                          device='DA14695', seen=seen)
        self.assertEqual(recorder.status, 200)
        self.assertEqual((seen['start'], seen['length']), (self.XIP, 0x200000))
        self.assertEqual(recorder.payload['erase_range']['source'], 'request')
        self.assertEqual(recorder.payload['erase_range']['text'],
                         '0x16000000-0x161FFFFF (2 MiB)')

    def test_half_a_pair_is_a_400_and_nothing_runs(self):
        for request in ({'erase_start': self.XIP}, {'erase_size': 0x200000}):
            with self.subTest(request=request):
                seen = {}
                recorder = _erase(ERASE_OK, request, seen=seen)
                self.assertEqual(recorder.status, 400)
                self.assertIn('together', recorder.payload['error'])
                self.assertEqual(seen, {}, 'chip_erase must not run')

    def test_a_range_outside_the_da1469x_window_is_a_400(self):
        seen = {}
        recorder = _erase(ERASE_OK, {'erase_start': 0x15000000, 'erase_size': 0x1000},
                          device='DA14695', seen=seen)
        self.assertEqual(recorder.status, 400)
        self.assertIn('QSPI XIP window', recorder.payload['error'])
        self.assertEqual(seen, {})

    def test_the_same_range_is_fine_on_another_part(self):
        seen = {}
        recorder = _erase(ERASE_OK, {'erase_start': 0x15000000, 'erase_size': 0x1000}, seen=seen)
        self.assertEqual(recorder.status, 200)
        self.assertEqual((seen['start'], seen['length']), (0x15000000, 0x1000))

    def test_non_integer_values_are_a_400(self):
        for request in ({'erase_start': '0x16000000', 'erase_size': 0x1000},
                        {'erase_start': self.XIP, 'erase_size': True}):
            with self.subTest(request=request):
                recorder = _erase(ERASE_OK, request)
                self.assertEqual(recorder.status, 400)

    def test_health_lists_the_feature(self):
        self.assertIn('erase_range', service.SERVICE_FEATURES)


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

    def test_silent_output_is_not_an_erase(self):
        # This used to be a 200, on the grounds that unrecognised output must
        # not newly fail. On hardware, a J-Link that dropped off USB and a
        # probe taken by another client both left Commander output with no
        # erase in it, and "Erase complete" was printed over an untouched
        # part. Every J-Link version lager ships prints `Erasing done.`
        # after an erase, so its absence is the failure.
        recorder = _erase([])
        self.assertEqual(recorder.status, 500)
        self.assertIn(api.NO_ERASE_DONE, recorder.payload['error'])

    def test_a_range_erase_confirmation_is_an_erase(self):
        recorder = _erase(['Erasing selected range...',
                           'Flash sectors within Range [0x16000000 - 0x160FFFFF] deleted.'])
        self.assertEqual(recorder.status, 200)


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
