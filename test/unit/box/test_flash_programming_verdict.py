# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""The box's verdict on a J-Link flash that programmed nothing.

J-Link Commander prints ``Downloading file [...]`` and only then downloads the
RAMCode it programs flash with. When that download fails it prints
``Failed to download RAMCode!`` and ``Unspecified error -1``, exits normally,
and nothing is programmed. ``flash_device()`` used to carry straight on: on a
DA1469x it ran the post-flash reset and yielded "Target reset -- bootrom will
reinitialise and boot application", and ``/debug/flash`` answered 200 with no
verdict, so the CLI printed "Flashed!" over a blank part.

Pinned here:

* ``flash_device()`` returns the failing line as its generator return value,
  judged on the flash session's own Commander output only;
* a DA1469x whose programming failed is not reset, and says so;
* ``/debug/flash`` reports ``programmed`` / ``error`` on both backends,
  still as a 200 so older CLIs keep working.
"""

import base64
import contextlib
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

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box'))
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.debug import api, service  # noqa: E402
from lager.debug.probes import BACKEND_JLINK, BACKEND_OPENOCD  # noqa: E402

DA1469X = 'DA14695'
OTHER = 'NRF52840_XXAA'

# Commander output from a DA1469x bench whose RAMCode download failed. Note
# `Downloading file` first: the failure comes after it.
RAMCODE_VERIFY_FAILED = """\
Downloading file [/tmp/tmp72n8yao0.bin]...
****** Error: Verification of RAMCode failed @ address 0x0080073C.
Write: 0x23009306 00039309
Read: 0x91804986 00039309
Failed to prepare for programming.
Failed to download RAMCode!
Error while determining flash info (Bank @ 0x16000000)
Unspecified error -1
"""

# The same failure reading back all zeros.
RAMCODE_READ_ZEROS = """\
Downloading file [/tmp/tmpa1b2c3d4.bin]...
****** Error: Verification of RAMCode failed @ address 0x0080073C.
Write: 0x401D6541 D0092E00
Read: 0x00000000 00000000
Failed to prepare for programming.
Failed to download RAMCode!
Error while determining flash info (Bank @ 0x16000000)
Unspecified error -1
"""

PROGRAMMED = """\
Downloading file [/tmp/tmpbh7c0j19.bin]...
J-Link: Flash download: Bank 0 @ 0x16000000: 1 range affected (4096 bytes)
J-Link: Flash download: Program speed: 222 KB/s
O.K.
"""

ATTACH_FAILED = """\
Connecting to target via SWD
Error occurred: Could not connect to the target device.
"""


# DA1469x SYS_CTRL_REG software reset: what the post-flash reset writes.
_SW_RESET = 'w4 0x100C0050 1'


@contextlib.contextmanager
def _bench(commander_output, reset_stat='00000000', jlink_procs=()):
    """flash_device() with the probe faked: JLink.flash yields
    `commander_output`; every later Commander command is recorded, and a
    `mem32` of RESET_STAT_REG answers `reset_stat`."""
    rec = types.SimpleNamespace(commands=[], gdbserver_starts=0)

    def fake_flash(self, files, preverify=False, verify=False, **kw):
        yield commander_output

    def run_command(cmd):
        rec.commands.append(cmd)
        if cmd.startswith('mem32 0x500000bc'):
            return f'500000BC = {reset_stat} \r\n'
        return 'O.K.'

    @contextlib.contextmanager
    def fake_commander(*args, **kwargs):
        jl = MagicMock()
        jl.run_command.side_effect = run_command
        yield jl

    def fake_start(**kwargs):
        rec.gdbserver_starts += 1

    with patch.object(api.JLink, 'flash', fake_flash), \
         patch.object(api, 'commander', fake_commander), \
         patch.object(api, '_jlink_processes', lambda serial: list(jlink_procs)), \
         patch.object(api, 'start_jlink_gdbserver', side_effect=fake_start), \
         patch.object(api, 'stop_jlink'), \
         patch.object(api, 'stop_jlink_gdbserver'), \
         patch.object(api.time, 'sleep'):
        yield rec


def _drain(gen):
    """(yielded lines, return value) of a generator."""
    lines = []
    while True:
        try:
            lines.append(next(gen))
        except StopIteration as done:
            return lines, done.value


def _flash(device, commander_output, **bench):
    with _bench(commander_output, **bench) as rec:
        lines, failure = _drain(api.flash_device(
            ([], [('/tmp/fw.bin', 0x16000000)], []), mcu=device))
    return rec, lines, failure


class FlashDeviceVerdictTests(unittest.TestCase):
    def test_a_failed_ramcode_download_is_the_verdict(self):
        _rec, _lines, failure = _flash(DA1469X, RAMCODE_VERIFY_FAILED)
        self.assertEqual(
            failure,
            '****** Error: Verification of RAMCode failed @ address 0x0080073C.')

    def test_the_all_zeros_read_is_the_same_verdict(self):
        _rec, _lines, failure = _flash(DA1469X, RAMCODE_READ_ZEROS)
        self.assertIn('Verification of RAMCode failed', failure)

    def test_each_failure_line_alone_is_enough(self):
        for line in ('Failed to download RAMCode!',
                     'Failed to download RAMCode.',
                     'Failed to prepare for programming.',
                     'Error while determining flash info (Bank @ 0x16000000)',
                     'ERROR: Verification of RAMCode failed @ address 0x0080073C.',
                     'Error while programming flash: Programming failed.'):
            with self.subTest(line=line):
                output = f'Downloading file [/tmp/fw.bin]...\r\n{line}\r\n'
                self.assertEqual(api._flash_failure([output]), line)

    def test_ramcode_used_for_other_purposes_is_not_a_flash_failure(self):
        """J-Link prints these for memory access and FPU registers; they say
        nothing about whether flash was programmed."""
        for line in ('Failed to download RAMCode for indirect memory access!',
                     'Failed to download RAMCode used to read FPU registers.'):
            with self.subTest(line=line):
                self.assertIsNone(api._flash_failure([PROGRAMMED + line + '\n']))

    def test_a_signature_mid_line_is_not_a_match(self):
        self.assertIsNone(api._flash_failure(
            ['note: the text "Failed to download RAMCode!" appears in this log\n']))

    def test_a_verify_failure_is_not_a_verdict(self):
        """Out of scope on purpose: on a DA1469x the cached-XIP compare reports
        a false one on a correctly programmed part."""
        self.assertIsNone(api._flash_failure(
            ['Downloading file [img.bin]...\n'
             'J-Link: Flash download: Bank 0 @ 0x16000000: 1 range affected\n'
             'Verification failed @ address 0x16020000.\n']))

    def test_a_clean_flash_has_no_verdict(self):
        _rec, _lines, failure = _flash(DA1469X, PROGRAMMED)
        self.assertIsNone(failure)

    def test_an_attach_failure_is_the_verdict(self):
        _rec, _lines, failure = _flash(OTHER, ATTACH_FAILED)
        self.assertEqual(failure, 'Error occurred: Could not connect to the target device.')

    def test_a_plain_for_loop_still_works(self):
        """The Net API and the README drive flash_device() with `for`."""
        with _bench(RAMCODE_VERIFY_FAILED):
            lines = list(api.flash_device(([], [('/tmp/fw.bin', 0x16000000)], []),
                                          mcu=DA1469X))
        self.assertIn(RAMCODE_VERIFY_FAILED, lines)


class Da1469xPostFlashResetTests(unittest.TestCase):
    def test_a_failed_program_is_not_reset(self):
        rec, lines, _failure = _flash(DA1469X, RAMCODE_VERIFY_FAILED)
        self.assertNotIn(_SW_RESET, rec.commands)
        self.assertIn('DA1469x: programming failed; skipping the post-flash reset', lines)
        self.assertFalse(any('Target reset' in line for line in lines), lines)
        self.assertFalse(any('resetting target' in line for line in lines), lines)

    def test_a_clean_flash_is_still_reset(self):
        rec, lines, _failure = _flash(DA1469X, PROGRAMMED)
        self.assertIn(_SW_RESET, rec.commands)
        self.assertTrue(any('Target reset' in line for line in lines), lines)

    def test_other_targets_still_reconnect_the_gdbserver_after_a_failure(self):
        rec, lines, failure = _flash(OTHER, RAMCODE_VERIFY_FAILED)
        self.assertIsNotNone(failure)
        self.assertEqual(rec.gdbserver_starts, 1)
        self.assertNotIn(_SW_RESET, rec.commands)
        self.assertIn('Reconnecting GDB server...', lines)


# A second J-Link client was driving the probe, so this Commander session could
# not use it at all -- captured on a DA1469x bench, where it printed "Flashed!"
# with nothing written.
PROBE_UNUSABLE = """\
Selected interface (SWD) is not supported by the connected probe.
Downloading file [/tmp/tmpq1w2e3r4.bin]...
Target connection not established yet but required for command.
"""


class UnusableProbeAndMissingDownloadTests(unittest.TestCase):
    def test_an_unusable_probe_is_the_verdict(self):
        self.assertEqual(
            api._flash_failure([PROBE_UNUSABLE]),
            'Selected interface (SWD) is not supported by the connected probe.')

    def test_each_unusable_line_fails_erase_and_flash(self):
        for line in ('Selected interface (SWD) is not supported by the connected probe.',
                     'Target connection not established yet but required for command.',
                     'J-Link connection not established yet but required for command.',
                     'Connecting to J-Link via USB...FAILED'):
            with self.subTest(line=line):
                self.assertTrue(api._attach_failed([line]))
                self.assertEqual(api._flash_failure([PROGRAMMED + line]), line)

    def test_downloading_without_a_flash_download_is_the_verdict(self):
        self.assertEqual(api._flash_failure(['Downloading file [a.bin]...\nO.K.\n']),
                         api.NO_FLASH_DOWNLOAD)

    def test_every_file_needs_its_own_flash_download(self):
        output = PROGRAMMED + 'Downloading file [b.bin]...\nO.K.\n'
        self.assertEqual(api._flash_failure([output]), api.NO_FLASH_DOWNLOAD)

    def test_a_skipped_bank_is_a_flash_download(self):
        self.assertIsNone(api._flash_failure([
            'Downloading file [a.bin]...\n'
            'J-Link: Flash download: Bank 0 @ 0x16000000: Skipped. Contents already match\n']))

    def test_an_unusable_probe_skips_the_reset_and_is_diagnosed(self):
        rec, lines, failure = _flash(DA1469X, PROBE_UNUSABLE)
        self.assertIsNotNone(failure)
        self.assertNotIn(_SW_RESET, rec.commands)
        self.assertIn('DA1469x: programming failed; skipping the post-flash reset', lines)
        self.assertTrue(any(line.startswith('Diagnosis:') for line in lines), lines)


class FailureDiagnosisTests(unittest.TestCase):
    """A failed flash says which of the two known causes was present: a
    second J-Link client on the probe, or a target reset mid-programming."""

    def test_a_watchdog_reset_during_programming_is_named(self):
        _rec, lines, _f = _flash(DA1469X, RAMCODE_READ_ZEROS, reset_stat='00000008')
        self.assertIn('Diagnosis: the target reset during programming '
                      '(RESET_STAT_REG=0x00000008: SYS watchdog).', lines)

    def test_several_reset_causes_are_all_named(self):
        _rec, lines, _f = _flash(DA1469X, RAMCODE_READ_ZEROS, reset_stat='00000024')
        self.assertIn('Diagnosis: the target reset during programming '
                      '(RESET_STAT_REG=0x00000024: software, CMAC watchdog).', lines)

    def test_no_reset_is_said_plainly(self):
        _rec, lines, _f = _flash(DA1469X, RAMCODE_VERIFY_FAILED)
        self.assertIn('Diagnosis: the target did not reset during programming '
                      '(RESET_STAT_REG=0x00000000).', lines)

    def test_another_jlink_client_is_listed(self):
        proc = '4242 /opt/SEGGER/JLink/JLinkExe -SelectEmuBySN 000123456789'
        _rec, lines, _f = _flash(DA1469X, RAMCODE_VERIFY_FAILED, jlink_procs=[proc])
        self.assertTrue(any(line.startswith('Diagnosis: another J-Link client')
                            for line in lines), lines)
        self.assertIn(f'  {proc}', lines)

    def test_no_other_client_is_said_plainly(self):
        _rec, lines, _f = _flash(DA1469X, RAMCODE_VERIFY_FAILED)
        self.assertIn('Diagnosis: no other J-Link client is using this probe.', lines)

    def test_diagnosis_comes_before_the_post_flash_steps(self):
        """The non-DA1469x reconnect starts a J-Link client of its own; the
        process list must be taken before it."""
        _rec, lines, _f = _flash(OTHER, RAMCODE_VERIFY_FAILED)
        diag = lines.index('Diagnosis: no other J-Link client is using this probe.')
        self.assertLess(diag, lines.index('Reconnecting GDB server...'))

    def test_other_targets_do_not_read_the_da1469x_register(self):
        rec, lines, _f = _flash(OTHER, RAMCODE_VERIFY_FAILED)
        self.assertFalse(any(c.startswith('mem32') for c in rec.commands))
        self.assertFalse(any('RESET_STAT_REG' in line for line in lines))

    def test_a_clean_flash_runs_no_diagnosis(self):
        rec, lines, _f = _flash(DA1469X, PROGRAMMED)
        self.assertFalse(any(line.startswith('Diagnosis:') for line in lines))
        self.assertFalse(any(c.startswith('mem32') for c in rec.commands))

    def test_an_unreadable_register_does_not_mask_the_failure(self):
        @contextlib.contextmanager
        def broken_commander(*a, **kw):
            raise RuntimeError('probe gone')
            yield  # pragma: no cover

        with patch.object(api, 'commander', broken_commander):
            value, reason = api._da1469x_reset_causes(['-device', DA1469X], None, None)
        self.assertIsNone(value)
        self.assertIn('probe gone', reason)


class JLinkProcessScanTests(unittest.TestCase):
    def _proc(self, entries):
        import tempfile
        root = tempfile.mkdtemp()
        for pid, argv in entries.items():
            os.makedirs(os.path.join(root, str(pid)))
            with open(os.path.join(root, str(pid), 'cmdline'), 'wb') as f:
                f.write(b'\0'.join(a.encode() for a in argv) + b'\0')
        os.makedirs(os.path.join(root, 'self'))
        return root

    def test_only_jlink_processes_on_this_probe_are_listed(self):
        root = self._proc({
            10: ['/opt/SEGGER/JLink/JLinkGDBServerCLExe', '-select', 'USB=111'],
            11: ['/opt/SEGGER/JLink/JLinkExe', '-SelectEmuBySN', '222'],
            12: ['python3', 'mentions JLinkExe and 111'],
        })
        self.assertEqual(api._jlink_processes('111', proc_root=root),
                         ['10 /opt/SEGGER/JLink/JLinkGDBServerCLExe -select USB=111'])

    def test_no_serial_lists_every_jlink_process(self):
        root = self._proc({
            10: ['/opt/SEGGER/JLink/JLinkGDBServerCLExe', '-select', 'USB'],
            11: ['/opt/SEGGER/JLink/JLinkExe'],
        })
        self.assertEqual(len(api._jlink_processes(None, proc_root=root)), 2)

    def test_a_missing_proc_is_an_empty_list(self):
        self.assertEqual(api._jlink_processes('111', proc_root='/nonexistent-proc'), [])


# ---- /debug/flash ------------------------------------------------------------

class _Run:
    def __init__(self):
        self.status = None
        self.payload = None

    def send_json_response(self, status_code, data):
        self.status = status_code
        self.payload = data


def _handler(run):
    """A DebugServiceHandler recording responses instead of writing them."""
    handler = service.DebugServiceHandler.__new__(service.DebugServiceHandler)
    handler.send_json_response = run.send_json_response
    handler.send_error_response = lambda code, message: (
        run.send_json_response(code, {'error': message, 'status': 'error'}))
    return handler


def _service_jlink_flash(lines, failure):
    def fake_flash_device(files, **kwargs):
        yield from lines
        return failure

    run = _Run()
    with patch.object(service.safety, 'check_destructive', lambda net, op: None), \
         patch.object(service, '_resolve_device_type', lambda net: OTHER), \
         patch.object(service, 'resolve_backend', lambda net: BACKEND_JLINK), \
         patch.object(service, '_resolve_probe',
                      lambda net: ('000123456789', 0, 2331, 2332, 2333, 9090)), \
         patch.object(service, 'get_jlink_gdbserver_status',
                      lambda serial=None: {'running': False}), \
         patch.object(service, '_get_script_file', lambda net: None), \
         patch.object(service, 'flash_device', fake_flash_device):
        _handler(run).handle_flash({
            'net': {'name': 'SWD', 'role': 'debug'},
            'hexfile': {'content': base64.b64encode(b':00000001FF\n').decode()},
        })
    return run


class DebugFlashResponseTests(unittest.TestCase):
    def test_a_failed_program_is_reported_as_not_programmed(self):
        line = 'Failed to download RAMCode!'
        run = _service_jlink_flash(['Flashing...', RAMCODE_VERIFY_FAILED], line)
        # Still a 200: an older CLI reads the output as it always has.
        self.assertEqual(run.status, 200)
        self.assertEqual(run.payload['status'], 'flash_complete')
        self.assertIs(run.payload['programmed'], False)
        self.assertEqual(run.payload['error'], line)
        self.assertEqual(run.payload['output'], ['Flashing...', RAMCODE_VERIFY_FAILED])

    def test_a_clean_flash_is_reported_as_programmed(self):
        run = _service_jlink_flash(['Flashing...', PROGRAMMED], None)
        self.assertEqual(run.status, 200)
        self.assertIs(run.payload['programmed'], True)
        self.assertIsNone(run.payload['error'])

    def test_a_flash_device_returning_a_list_still_works(self):
        """A stand-in that returns a list, not a generator, has no verdict."""
        run = _Run()
        with patch.object(service.safety, 'check_destructive', lambda net, op: None), \
             patch.object(service, '_resolve_device_type', lambda net: OTHER), \
             patch.object(service, 'resolve_backend', lambda net: BACKEND_JLINK), \
             patch.object(service, '_resolve_probe',
                          lambda net: ('000123456789', 0, 2331, 2332, 2333, 9090)), \
             patch.object(service, 'get_jlink_gdbserver_status',
                          lambda serial=None: {'running': False}), \
             patch.object(service, '_get_script_file', lambda net: None), \
             patch.object(service, 'flash_device', lambda files, **kw: ['O.K.']):
            _handler(run).handle_flash({
                'net': {'name': 'SWD', 'role': 'debug'},
                'hexfile': {'content': base64.b64encode(b':00000001FF\n').decode()},
            })
        self.assertEqual(run.status, 200)
        self.assertIs(run.payload['programmed'], True)

    def test_openocd_reports_programmed_on_success(self):
        """A failed OpenOCD flash raises and answers 500, so a 200 means it
        programmed."""
        rpc = MagicMock()
        run = _Run()
        with patch.object(service.safety, 'check_destructive', lambda net, op: None), \
             patch.object(service, '_resolve_device_type', lambda net: OTHER), \
             patch.object(service, 'resolve_backend', lambda net: BACKEND_OPENOCD), \
             patch.object(service, '_resolve_probe',
                          lambda net: ('FT4232H01', 0, 2331, 2332, 2333, 9090)), \
             patch.object(service, '_openocd_ports_for_slot', lambda slot: (4444, 6666)), \
             patch.object(service, 'get_openocd_status',
                          lambda serial=None: {'running': True, 'pid': 9}), \
             patch.object(service, 'OpenOcdRpc', lambda **kw: rpc), \
             patch.object(service, 'flash_target', lambda *a, **kw: iter(['wrote 4 bytes'])):
            _handler(run).handle_flash({
                'net': {'name': 'SWD', 'role': 'debug'},
                'hexfile': {'content': base64.b64encode(b':00000001FF\n').decode()},
            })
        self.assertEqual(run.status, 200, run.payload)
        self.assertIs(run.payload['programmed'], True)
        self.assertIsNone(run.payload['error'])


class ProbeEndpointLockTests(unittest.TestCase):
    """The probe-driving endpoints run whole under the probe lock. /debug/connect
    used to check status, stop the running server on a force reconnect and
    start a new one outside it, and a flash in between had its GDB server
    stopped under it."""

    def _post(self, path, body, **patches):
        import io
        import json as _json
        run = _Run()
        handler = _handler(run)
        raw = _json.dumps(body).encode()
        handler.path = path
        handler.headers = {'Content-Length': str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(patch.object(service, name, value))
            handler.do_POST()
        return run

    def test_each_probe_endpoint_runs_under_its_probes_lock(self):
        seen = {}

        def recorder(operation):
            def handle(self_, data):
                seen[operation] = held(operation)
                self_.send_json_response(200, {'status': 'ok'})
            return handle

        held_now = []

        @contextlib.contextmanager
        def fake_lock(serial, operation):
            held_now.append((serial, operation))
            yield

        def held(operation):
            return (('000123456789', operation) in held_now)

        net = {'name': 'SWD', 'address': 'USB::0x1366::0x0101::000123456789::INSTR'}
        for path, operation in service._PROBE_LOCKED_ENDPOINTS.items():
            with self.subTest(path=path), \
                 patch.object(service.DebugServiceHandler, f'handle_{operation}',
                              recorder(operation)):
                run = self._post(path, {'net': net}, probe_lock=fake_lock,
                                 resolve_serial_from_net=lambda n: '000123456789')
                self.assertEqual(run.status, 200)
        self.assertEqual(set(seen), set(service._PROBE_LOCKED_ENDPOINTS.values()))
        self.assertTrue(all(seen.values()), seen)

    def test_rtt_is_not_locked(self):
        self.assertNotIn('/debug/rtt', service._PROBE_LOCKED_ENDPOINTS)

    def test_a_busy_probe_answers_503_naming_the_holder(self):
        @contextlib.contextmanager
        def busy(serial, operation):
            raise service.ProbeBusyError('J-Link probe 111 is busy with flash (pid 7)')
            yield  # pragma: no cover

        with patch.object(service.DebugServiceHandler, 'handle_connect',
                          lambda self_, data: self.fail('handler ran')):
            run = self._post('/debug/connect', {'net': {'name': 'SWD'}}, probe_lock=busy)
        self.assertEqual(run.status, 503)
        self.assertIn('busy with flash', run.payload['error'])

    def test_a_net_that_does_not_resolve_still_reaches_the_handler(self):
        def boom(net):
            raise ValueError('bad address')

        with patch.object(service.DebugServiceHandler, 'handle_memrd',
                          lambda self_, data: self_.send_json_response(200, {})):
            run = self._post('/debug/memrd', {'net': {'name': 'SWD'}},
                             resolve_serial_from_net=boom)
        self.assertEqual(run.status, 200)


if __name__ == '__main__':
    unittest.main()
