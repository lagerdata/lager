# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""`/debug/status` must distinguish a live gdbserver from an attached target.

The endpoint reported one boolean, `connected`, and it meant "the gdbserver PID
is alive". `_auto_connect_if_needed` short-circuited on it, so on a box where
the server outlives the target every debug subcommand -- flash, reset, memrd,
erase, the RTT paths -- proceeded against hardware that was not there believing
it was connected. #344 fixed the erase verdict at one call site by reading the
programmer's output; this is the shared cause underneath it.

`handle_debug_status` had no unit test at all before this file.

The tri-state is the part worth pinning. None is not False: an older box, a
gdbserver refusing a second GDB client because a user is already attached, or a
probe that times out are all "could not establish", and reading any of them as
"absent" would tear down working sessions.

Module stubs follow test_debug_erase_verdict.py -- `setdefault`, never a
meta_path hook, so a real dependency that IS installed keeps winning.
"""

import os
import pathlib
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


for _dep in ['pyvisa', 'pyvisa.constants', 'usb', 'usb.util', 'usb.core', 'pigpio',
             'labjack', 'labjack.ljm', 'nidaqmx', 'bleak', 'serial',
             'serial.tools', 'serial.tools.list_ports',
             'pygdbmi', 'pygdbmi.gdbcontroller', 'pygdbmi.constants']:
    _stub(_dep)

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')))

from lager.debug import service, target_probe  # noqa: E402


# A real J-Link log from a session that came up against nothing.
LOG_ATTACH_FAILED = """
SEGGER J-Link GDB Server V7.88 Command Line Version
Connecting to target...
AP[0]: Skipped. Could not read CPUID register
Failed to power up DAP
ERROR: Could not connect to target.
"""

LOG_HEALTHY = """
SEGGER J-Link GDB Server V7.88 Command Line Version
Connecting to target...
Cortex-M33 identified.
Listening on port 2331 for gdb connections
"""

# `x/4xb 0xe000ed00` against an attached Cortex-M33.
GDB_CPUID_OK = [
    {'type': 'console', 'payload': '0xe000ed00 <SCB>:\\t0x04\\t0x00\\t0x0c\\t0x41'},
]
GDB_CPUID_UNREACHABLE = [
    {'type': 'console', 'payload': 'Cannot access memory at address 0xe000ed00'},
]
GDB_SILENT = []


class _Recorder:
    def __init__(self):
        self.status = None
        self.payload = None

    def send_json_response(self, status_code, data):
        self.status = status_code
        self.payload = data


def _handler():
    handler = service.DebugServiceHandler.__new__(service.DebugServiceHandler)
    recorder = _Recorder()
    handler.send_json_response = recorder.send_json_response
    handler.send_error_response = lambda code, message: (
        recorder.send_json_response(code, {'error': message, 'status': 'error'}))
    return handler, recorder


def _status(running, attached, probe=False):
    """Drive handle_debug_status with a canned server state and probe verdict."""
    handler, recorder = _handler()
    with patch.object(service, 'resolve_backend', lambda net: service.BACKEND_JLINK), \
         patch.object(service, '_resolve_probe',
                      lambda net: ('000051014439', 0, 2331, 2332, 2333, 2334)), \
         patch.object(service, '_resolve_device_type', lambda net: 'NRF5340_XXAA_APP'), \
         patch.object(service, 'get_jlink_gdbserver_status',
                      lambda serial=None: {'running': running, 'pid': 4242 if running else None}), \
         patch.object(service, 'target_attached', lambda *a, **k: attached):
        handler.handle_debug_status({'net': {'name': 'debug1'}, 'probe': probe})
    return recorder.payload


class StatusReportsTwoStates(unittest.TestCase):

    def test_a_dead_server_means_nothing_is_attached(self):
        payload = _status(running=False, attached=None)
        self.assertIs(payload['gdbserver_running'], False)
        self.assertIs(payload['target_attached'], False)
        self.assertIsNone(payload['pid'])

    def test_a_live_server_with_an_answering_target(self):
        payload = _status(running=True, attached=True)
        self.assertIs(payload['gdbserver_running'], True)
        self.assertIs(payload['target_attached'], True)
        self.assertEqual(payload['pid'], 4242)

    def test_the_bug_a_live_server_with_an_absent_target(self):
        """The case the old single boolean could not express."""
        payload = _status(running=True, attached=False)
        self.assertIs(payload['gdbserver_running'], True)
        self.assertIs(payload['target_attached'], False)

    def test_an_inconclusive_probe_stays_none(self):
        payload = _status(running=True, attached=None)
        self.assertIsNone(payload['target_attached'])

    def test_connected_keeps_its_historical_meaning(self):
        """The alias must track the server, not the target.

        An older CLI reads `connected` and acts on it. Repointing it at the
        target would silently change that client's behaviour; keeping it on the
        server means an old CLI behaves exactly as it does today.
        """
        for attached in (True, False, None):
            with self.subTest(attached=attached):
                payload = _status(running=True, attached=attached)
                self.assertIs(payload['connected'], payload['gdbserver_running'])


class LogScrapeIsTheCheapPath(unittest.TestCase):
    """A recorded attach failure is decisive and costs no wire access."""

    def _with_log(self, contents, probe=False, gdb=None):
        """Hand the code a real logfile in a temp dir.

        Deliberately not a mock over the filesystem probe: `import os` hands
        every module the same os.path object, so patching its members is
        process-global rather than scoped, and on 3.14 it also neuters every
        Path.exists() in the process. test/unit/test_no_global_os_path_patches.py
        forbids it -- including in prose, since it scans file text. The seam
        here is the logfile-path helper, so a real file needs no filesystem
        patching at all.
        """
        with tempfile.TemporaryDirectory() as tmp:
            logfile = pathlib.Path(tmp) / 'jlink_gdbserver_test.log'
            logfile.write_text(contents)
            with patch.object(target_probe, 'jlink_gdbserver_logfile', lambda s: str(logfile)), \
                 patch.object(target_probe, '_probe_over_gdb', lambda device, port: gdb):
                return target_probe.target_attached(
                    'jlink', '000051014439', gdb_port=2331,
                    device='NRF5340_XXAA_APP', probe=probe)

    def test_a_recorded_attach_failure_is_false_without_probing(self):
        self.assertIs(self._with_log(LOG_ATTACH_FAILED), False)

    def test_a_healthy_log_alone_does_not_prove_attachment(self):
        """The log says the server came up, not that the part is still there."""
        self.assertIsNone(self._with_log(LOG_HEALTHY))

    def test_probing_is_opt_in(self):
        """Without probe=True no wire access happens, so the answer stays None.

        /debug/status is called by every debug subcommand; an unconditional GDB
        round trip would put seconds on each one.
        """
        self.assertIsNone(self._with_log(LOG_HEALTHY, probe=False, gdb=True))

    def test_probing_reports_the_targets_answer(self):
        self.assertIs(self._with_log(LOG_HEALTHY, probe=True, gdb=True), True)
        self.assertIs(self._with_log(LOG_HEALTHY, probe=True, gdb=False), False)
        self.assertIsNone(self._with_log(LOG_HEALTHY, probe=True, gdb=None))

    def test_a_missing_logfile_is_not_a_failure(self):
        """A path inside a temp dir that was never written: genuinely absent."""
        with tempfile.TemporaryDirectory() as tmp:
            absent = str(pathlib.Path(tmp) / 'never_written.log')
            with patch.object(target_probe, 'jlink_gdbserver_logfile', lambda s: absent):
                self.assertIsNone(target_probe.target_attached(
                    'jlink', 'SERIAL', gdb_port=2331, probe=False))


class GdbProbeParsing(unittest.TestCase):

    def _probe(self, responses):
        with patch('lager.debug.gdb.read_memory', lambda *a, **k: responses):
            return target_probe._probe_over_gdb('NRF5340_XXAA_APP', 2331)

    def test_a_memory_row_means_attached(self):
        self.assertIs(self._probe(GDB_CPUID_OK), True)

    def test_an_access_error_means_not_attached(self):
        self.assertIs(self._probe(GDB_CPUID_UNREACHABLE), False)

    def test_silence_is_not_an_answer(self):
        self.assertIsNone(self._probe(GDB_SILENT))

    def test_a_refused_controller_is_not_evidence(self):
        """A second GDB client can be refused while a user is attached.

        That says nothing about the target, so it must not read as absent.
        """
        def boom(*a, **k):
            raise RuntimeError('connection refused')
        with patch('lager.debug.gdb.read_memory', boom):
            self.assertIsNone(target_probe._probe_over_gdb('DEV', 2331))


class OpenOcdProbeParsing(unittest.TestCase):

    def test_an_empty_rpc_reply_is_not_evidence(self):
        """OpenOCD's RPC returning '' is a known, still-open behaviour."""
        rpc = MagicMock()
        rpc.__enter__ = lambda self: self
        rpc.__exit__ = lambda self, *a: False
        rpc.mdw = lambda addr, count: ''
        with patch('lager.debug.openocd.OpenOcdRpc', lambda port: rpc):
            self.assertIsNone(target_probe._probe_over_openocd(6666))


if __name__ == '__main__':
    unittest.main()
