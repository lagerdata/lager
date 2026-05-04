# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""
No-DUT smoke test for the multi-probe ``start_jlink_gdbserver`` path.

Stubs the hardware libraries (pyvisa / usb / pexpect / etc.) the way
``test_hardware_service_retry.py`` does, then patches ``subprocess.Popen`` and a
few helpers so we can drive ``start_jlink_gdbserver`` end-to-end without ever
spawning a real JLinkGDBServer or touching a real probe. The test then asserts
that two distinct serials produce:

* two ``JLinkGDBServerCLExe`` invocations with per-probe ``-select USB=<sn>``
* distinct ``-port`` and ``-RTTTelnetPort`` values
* distinct ``-log`` paths
* per-serial PID files written to disk

This catches regressions in the per-probe plumbing on every push, with no
hardware in the loop.
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
    'pigpio',
    'labjack', 'labjack.ljm',
    'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak',
    'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev',
    'smbus', 'smbus2',
    'RPi', 'RPi.GPIO',
    'gpiod',
    'pexpect', 'pexpect.replwrap', 'pexpect.exceptions',
    # Pulled in by lager.debug.gdb / api / __init__:
    'pygdbmi', 'pygdbmi.gdbcontroller', 'pygdbmi.constants',
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.debug import gdbserver as gdbserver_mod  # noqa: E402


class _FakeProc:
    """Minimal stand-in for the subprocess.Popen return value."""
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None  # still running — start_jlink_gdbserver treats this as success


class _TmpdirSandbox:
    """Redirect per-serial path helpers into a tmpdir so the test never writes /tmp.

    ``gdbserver.py`` imports the helpers by name (``from .probes import ...``), so
    we have to rebind them on the gdbserver module itself, not on probes.
    """

    def __enter__(self):
        self.tmpdir = tempfile.mkdtemp(prefix='lager_jlink_test_')
        self._orig_pid = gdbserver_mod.jlink_gdbserver_pidfile
        self._orig_log = gdbserver_mod.jlink_gdbserver_logfile
        gdbserver_mod.jlink_gdbserver_pidfile = lambda s: os.path.join(
            self.tmpdir, f'jlink_gdbserver_{s or "legacy"}.pid'
        )
        gdbserver_mod.jlink_gdbserver_logfile = lambda s: os.path.join(
            self.tmpdir, f'jlink_gdbserver_{s or "legacy"}.log'
        )
        return self

    def __exit__(self, *exc):
        gdbserver_mod.jlink_gdbserver_pidfile = self._orig_pid
        gdbserver_mod.jlink_gdbserver_logfile = self._orig_log


def _start(serial, gdb_port, rtt_port, fake_pid):
    """Drive start_jlink_gdbserver with everything risky mocked out."""
    with patch.object(gdbserver_mod, 'stop_jlink_gdbserver'), \
         patch('subprocess.Popen') as mock_popen, \
         patch('os.kill', return_value=None), \
         patch.object(gdbserver_mod, 'time'), \
         patch.object(gdbserver_mod, 'get_jlink_gdb_server_path',
                      return_value='/fake/path/JLinkGDBServerCLExe'):
        mock_popen.return_value = _FakeProc(fake_pid)
        result = gdbserver_mod.start_jlink_gdbserver(
            device='NRF52840_XXAA', speed='4000', transport='SWD',
            serial=serial, gdb_port=gdb_port, rtt_telnet_port=rtt_port,
        )
        cmd = mock_popen.call_args.args[0]
    return result, cmd


class TwoProbesProduceDistinctInvocations(unittest.TestCase):
    """Drive start_jlink_gdbserver twice with different serials; assert isolation."""

    def test_each_probe_gets_its_own_select_port_log_pidfile(self):
        with _TmpdirSandbox() as sandbox:
            r1, cmd1 = _start('000051014439', 2331, 9090, 11111)
            r2, cmd2 = _start('000051017734', 2332, 9092, 22222)

            # Each cmd carries a per-probe -select USB=<sn>
            self.assertEqual(cmd1[cmd1.index('-select') + 1], 'USB=000051014439')
            self.assertEqual(cmd2[cmd2.index('-select') + 1], 'USB=000051017734')

            # Distinct -port values
            self.assertEqual(cmd1[cmd1.index('-port') + 1], '2331')
            self.assertEqual(cmd2[cmd2.index('-port') + 1], '2332')

            # Distinct -RTTTelnetPort values
            self.assertEqual(cmd1[cmd1.index('-RTTTelnetPort') + 1], '9090')
            self.assertEqual(cmd2[cmd2.index('-RTTTelnetPort') + 1], '9092')

            # Distinct -log paths, each containing the serial
            log1 = cmd1[cmd1.index('-log') + 1]
            log2 = cmd2[cmd2.index('-log') + 1]
            self.assertNotEqual(log1, log2)
            self.assertIn('000051014439', log1)
            self.assertIn('000051017734', log2)

            # Per-serial PID files were written with the right contents
            pid1_path = os.path.join(sandbox.tmpdir, 'jlink_gdbserver_000051014439.pid')
            pid2_path = os.path.join(sandbox.tmpdir, 'jlink_gdbserver_000051017734.pid')
            with open(pid1_path) as f:
                self.assertEqual(f.read().strip(), '11111')
            with open(pid2_path) as f:
                self.assertEqual(f.read().strip(), '22222')

            # Returned dict surfaces the per-probe metadata
            self.assertEqual(r1['serial'], '000051014439')
            self.assertEqual(r1['gdb_port'], 2331)
            self.assertEqual(r2['serial'], '000051017734')
            self.assertEqual(r2['gdb_port'], 2332)


class LegacyPathStillWorks(unittest.TestCase):
    """``serial=None`` must still produce a valid bare ``-select USB`` invocation."""

    def test_legacy_serial_none_produces_bare_select_usb(self):
        with _TmpdirSandbox():
            _, cmd = _start(None, 2331, 9090, 99999)

        self.assertEqual(cmd[cmd.index('-select') + 1], 'USB')
        self.assertEqual(cmd[cmd.index('-port') + 1], '2331')
        self.assertEqual(cmd[cmd.index('-RTTTelnetPort') + 1], '9090')


if __name__ == '__main__':
    unittest.main()
