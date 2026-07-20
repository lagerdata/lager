# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for three debug-path defects that made an on-bench J-Link failure
much harder to diagnose than it should have been:

  1. validate_speed() returned the caller's value unchanged, so an int speed
     made the connect-error message's ``', '.join(speeds_to_try)`` raise
     ``TypeError: sequence item 0: expected str instance, int found`` — which
     REPLACED the real J-Link reason (e.g. "Failed to power up DAP") in the
     console.
  2. get_device() read ``os.environ['LAGER_BOX_COMMANDS']`` unguarded, raising a
     bare ``KeyError('LAGER_BOX_COMMANDS')`` that surfaced as the opaque
     "RTT auto-detection failed: 'LAGER_BOX_COMMANDS'" warning when a script is
     exec'd into the container directly (no such env var).
  3. start_jlink_gdbserver() freed the GDB port only via the serial-anchored
     stop, so a leftover gdbserver started under a different ``-select`` tag
     (e.g. without a serial) kept holding the port and the next connect died
     with "Failed to open listener port".

Heavy hardware deps are stubbed so lager.debug.* imports without a box.
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


for _dep in (
    'pyvisa', 'pyvisa.constants', 'pyvisa_py', 'usb', 'usb.util', 'usb.core',
    'pigpio', 'labjack', 'labjack.ljm', 'nidaqmx', 'phidget22',
    'phidget22.Phidget', 'phidget22.Net', 'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports', 'spidev',
    'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
    'pexpect', 'pexpect.replwrap', 'pexpect.exceptions',
    'pygdbmi', 'pygdbmi.gdbcontroller', 'pygdbmi.constants',
):
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.debug import api as debug_api  # noqa: E402
from lager.debug import gdb as debug_gdb  # noqa: E402
from lager.debug import gdbserver as debug_gdbserver  # noqa: E402


class ValidateSpeedReturnsStr(unittest.TestCase):
    """Bug 1: the returned speed must be a str so the error-message join can't
    raise TypeError and swallow the real J-Link diagnosis."""

    def test_int_speed_returns_str(self):
        self.assertEqual(debug_api.validate_speed(4000), '4000')
        self.assertIsInstance(debug_api.validate_speed(4000), str)

    def test_str_speed_returns_str(self):
        self.assertEqual(debug_api.validate_speed('1000'), '1000')

    def test_adaptive_passes_through(self):
        self.assertEqual(debug_api.validate_speed('adaptive'), 'adaptive')

    def test_error_message_join_never_raises_on_int_speeds(self):
        # Even if a caller slipped an int into the list, the message must render.
        speeds_to_try = [debug_api.validate_speed(1000), '500', 100]
        msg = f"tried {', '.join(str(s) for s in speeds_to_try)} kHz"
        self.assertEqual(msg, 'tried 1000, 500, 100 kHz')

    def test_invalid_speed_still_raises_valueerror(self):
        with self.assertRaises(ValueError):
            debug_api.validate_speed('fast')
        with self.assertRaises(ValueError):
            debug_api.validate_speed(0)


class GetDeviceClearError(unittest.TestCase):
    """Bug 2: an actionable message, not a bare KeyError, when the env var is
    absent (the state under an external test runner)."""

    def test_missing_env_raises_actionable_runtimeerror(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop('LAGER_BOX_COMMANDS', None)
            with self.assertRaises(RuntimeError) as ctx:
                debug_gdb.get_device()
        msg = str(ctx.exception)
        self.assertIn('LAGER_BOX_COMMANDS', msg)
        self.assertNotEqual(msg, "'LAGER_BOX_COMMANDS'")  # not a bare KeyError str
        self.assertIn('device', msg.lower())

    def test_malformed_json_raises_runtimeerror(self):
        with patch.dict(os.environ, {'LAGER_BOX_COMMANDS': 'not json'}):
            with self.assertRaises(RuntimeError):
                debug_gdb.get_device()

    def test_valid_env_returns_device(self):
        with patch.dict(
            os.environ,
            {'LAGER_BOX_COMMANDS': '{"jlink_device": "NRF52833_XXAA"}'},
        ):
            self.assertEqual(debug_gdb.get_device(), 'NRF52833_XXAA')


class FreeGdbPort(unittest.TestCase):
    """Bug 3: a gdbserver left under a different ``-select`` tag (e.g. without a
    serial) must be evicted before the next bind — the serial-anchored stop
    can't see it."""

    def test_kills_process_holding_the_exact_port(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            # pgrep 'found something', pkill returns 0
            rc = 0 if cmd[0] in ('pgrep', 'pkill') else 0
            return types.SimpleNamespace(returncode=rc, stdout=b'2735\n')

        with patch.object(debug_gdbserver.subprocess, 'run', side_effect=fake_run), \
             patch.object(debug_gdbserver.time, 'sleep'):
            debug_gdbserver._free_gdb_port(2331)

        # pgrep is `pgrep -f <pat>`, pkill is `pkill -TERM/-KILL -f <pat>`; the
        # pattern is always the last arg.
        patterns = [c[-1] for c in calls if c[0] in ('pgrep', 'pkill')]
        self.assertTrue(patterns, 'expected pgrep/pkill to run')
        # The port is matched with a trailing space so 2331 never matches 23310.
        self.assertTrue(all('-port 2331 ' in p for p in patterns))

    def test_noop_when_nothing_holds_the_port(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == 'pgrep':
                return types.SimpleNamespace(returncode=1, stdout=b'')  # none found
            raise AssertionError(f'pkill should not run: {cmd}')

        with patch.object(debug_gdbserver.subprocess, 'run', side_effect=fake_run), \
             patch.object(debug_gdbserver.time, 'sleep'):
            debug_gdbserver._free_gdb_port(2331)  # must not raise / must not pkill

    def test_missing_procps_does_not_crash_startup(self):
        # Best-effort cleanup: if neither pgrep nor pkill exists, _free_gdb_port
        # must swallow it rather than propagate FileNotFoundError up into
        # start_jlink_gdbserver.
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        with patch.object(debug_gdbserver.subprocess, 'run', side_effect=fake_run), \
             patch.object(debug_gdbserver.time, 'sleep'):
            debug_gdbserver._free_gdb_port(2331)  # must not raise


if __name__ == '__main__':
    unittest.main()
