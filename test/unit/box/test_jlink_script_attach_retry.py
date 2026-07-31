# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the J-Link script attach retry (issues #195, #186, #162).

A user `.JLinkScript` that defines ``InitTarget()`` REPLACES J-Link's built-in
per-device ``InitTarget()``. The replacement is **per function** -- a script
defining no ``InitTarget()`` leaves the built-in in place and is harmless.

On an nRF5340 that built-in is what brings the DAP up on a blank or protected
part. Measured with ``JLinkExe -log`` on a bench, connect only, programmed
target:

    | user script                | InitTarget() took | whose ran        |
    | none                       | 1.95 ms           | the device's     |
    | defines InitTarget()       | 14 us             | the user's       |
    | defines nothing (inert)    | 5.44 ms           | the device's     |

It only bites after an erase, where the built-in has to bring up a blank part
cold -- measured at 425-429 ms there, against 2-3 us for a user stub. End to end
through ``lager debug <net> flash``, three trials per cell, re-erased each time:

    | script configured          | result        |
    | none                       | Flashed!      |
    | defines InitTarget()       | Flash failed  |
    | defines no InitTarget()    | Flashed!      |

Because ``flash`` erases by default, one scripted flash left the part blank and
the net failing every later attach.

What is pinned here: the connect exhausts its speed ladder WITH the script
before dropping it, drops it only as a last resort, reports when it did, and
never silently succeeds without saying the script was skipped.
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


for _dep in ['pyvisa', 'pyvisa.constants', 'usb', 'usb.util', 'usb.core', 'pigpio',
             'labjack', 'labjack.ljm', 'nidaqmx', 'bleak', 'serial',
             'serial.tools', 'serial.tools.list_ports', 'pygdbmi']:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box'))
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.debug import api  # noqa: E402

SCRIPT = '/tmp/lager_jlink_script_debug1.JLinkScript'


class _Bench:
    """Records every start_jlink_gdbserver call and decides which succeed."""

    def __init__(self, succeed_when):
        self.calls = []
        self._succeed_when = succeed_when

    def start(self, **kwargs):
        self.calls.append(kwargs)
        if not self._succeed_when(kwargs):
            raise RuntimeError('Could not connect to target')
        return {'pid': 4242, 'gdb_port': kwargs.get('gdb_port', 2331),
                'rtt_telnet_port': kwargs.get('rtt_telnet_port', 9090)}

    @property
    def scripts_tried(self):
        return [c.get('script_file') for c in self.calls]


def _connect(bench, **overrides):
    """Drive connect_jlink against *bench*, stubbing everything hardware."""
    running = {'running': False}

    def _status(*a, **k):
        # "running" iff the most recent start() succeeded.
        return {'running': bool(bench.calls) and running['ok']}

    def _start(**kwargs):
        try:
            result = bench.start(**kwargs)
        except Exception:
            running['ok'] = False
            raise
        running['ok'] = True
        return result

    running['ok'] = False
    kwargs = dict(speed='4000', device='nRF5340_xxAA_APP', transport='SWD',
                  script_file=SCRIPT)
    kwargs.update(overrides)
    with patch.object(api, 'start_jlink_gdbserver', side_effect=_start), \
         patch.object(api, 'get_jlink_status', return_value={'running': False}), \
         patch.object(api, 'get_jlink_gdbserver_status', side_effect=_status), \
         patch.object(api, 'stop_jlink'), \
         patch.object(api, 'stop_jlink_gdbserver'), \
         patch.object(api, 'gdb_reset'), \
         patch.object(api.time, 'sleep'):
        return api.connect_jlink(**kwargs)


class ScriptRetryOrderTests(unittest.TestCase):
    """The script is dropped only after the speed ladder is exhausted with it."""

    def test_a_working_script_is_never_dropped(self):
        bench = _Bench(succeed_when=lambda k: True)
        status = _connect(bench)
        self.assertEqual(bench.scripts_tried, [SCRIPT])
        self.assertNotIn('script_skipped', status,
                         'nothing was skipped, so nothing should be reported')

    def test_the_script_is_kept_for_every_speed_before_being_dropped(self):
        """Dropping the user's script is the more surprising change, so every
        slower speed is tried with it first."""
        bench = _Bench(succeed_when=lambda k: k['script_file'] is None)
        _connect(bench, speed='4000')
        tried = bench.scripts_tried
        self.assertIsNone(tried[-1], 'the last attempt must be the scriptless one')
        self.assertTrue(all(s == SCRIPT for s in tried[:-1]),
                        f'script dropped too early: {tried}')
        self.assertGreater(len(tried), 2, 'the speed ladder should have run first')

    def test_dropping_the_script_is_reported(self):
        bench = _Bench(succeed_when=lambda k: k['script_file'] is None)
        status = _connect(bench)
        self.assertEqual(status['script_skipped'], SCRIPT)

    def test_no_configured_script_means_no_second_pass(self):
        bench = _Bench(succeed_when=lambda k: True)
        status = _connect(bench, script_file=None)
        self.assertEqual(bench.scripts_tried, [None])
        self.assertNotIn('script_skipped', status)

    def test_a_target_that_is_dead_either_way_still_raises(self):
        """The retry must not turn a genuinely unreachable target into a pass."""
        bench = _Bench(succeed_when=lambda k: False)
        with self.assertRaises(Exception):
            _connect(bench)
        self.assertIn(None, bench.scripts_tried,
                      'the scriptless attempt should still have been made')


class CommanderConnectFailureDetectionTests(unittest.TestCase):
    """J-Link Commander does not raise when it cannot attach.

    It prints and carries on, so the flash path -- which drives Commander, not
    the gdbserver -- can only notice by reading the output. This is the layer
    that actually fails after `flash` erases: the erase blanks the part, and the
    Commander connect that follows cannot attach with the device InitTarget()
    displaced. Getting these strings wrong means the retry never fires, which is
    exactly the bug, so they are pinned.
    """

    def test_the_reported_failure_text_is_detected(self):
        observed = [
            'Connecting to target via SWD',
            'AP[0]: Skipped. Could not read CPUID register',
            'Attach to CPU failed. Executing connect under reset.',
            'Failed to power up DAP',
            'ERROR: Could not connect to target.',
        ]
        self.assertTrue(api._connect_failed(observed))

    def test_each_variant_alone_is_enough(self):
        for line in ('Could not connect to the target device.',
                     'Cannot connect to target.',
                     'Failed to power up DAP',
                     'Could not read CPUID register'):
            with self.subTest(line=line):
                self.assertTrue(api._connect_failed([line]))

    def test_a_successful_flash_is_not_mistaken_for_a_failure(self):
        good = [
            'Connecting to target via SWD',
            'Found SW-DP with ID 0x6BA02477',
            'Cortex-M33 identified.',
            'J-Link: Flash download: Program: 0.019s',
            'O.K.',
        ]
        self.assertFalse(api._connect_failed(good))

    def test_empty_output_is_not_a_failure(self):
        self.assertFalse(api._connect_failed([]))


if __name__ == '__main__':
    unittest.main()
