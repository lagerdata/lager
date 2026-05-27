#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `lager diagnose` classification — the function that turns
three endpoint payloads into a one-line user-actionable diagnosis. This is
the core decision tree we want to pin down so a future "small refactor"
doesn't silently demote a "HOST-SIDE: usbtmc loaded" hit to "UNCLEAR".
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from cli.commands.box.diagnose import _classify


class ClassifyTests(unittest.TestCase):

    def test_usbtmc_loaded_wins_over_everything(self):
        """If the usbtmc kernel module is loaded, that's the root cause —
        flag it first regardless of what the visa/dispatcher endpoints say."""
        color, msg = _classify(
            usb_info={'usbtmc_loaded': True, 'enumerated': True, 'lsof': []},
            visa_info={'idn': 'KEITHLEY ...'},  # would be "healthy" otherwise
            disp_info={'cached_session': True},
        )
        self.assertEqual(color, 'red')
        self.assertIn('usbtmc kernel module loaded', msg)
        self.assertIn('lager box update', msg)

    def test_busy_with_multiple_holders_calls_out_pids(self):
        """When visa returns 'busy' AND lsof shows multiple processes
        holding the device, we should name the racing processes."""
        color, msg = _classify(
            usb_info={'enumerated': True, 'usbtmc_loaded': False,
                      'lsof': [
                          {'command': 'python3', 'pid': '12345'},
                          {'command': 'python3', 'pid': '99999'},
                      ]},
            visa_info={'error_class': 'busy', 'error': '[Errno 16] Resource busy'},
            disp_info={'cached_session': True},
        )
        self.assertEqual(color, 'red')
        self.assertIn('multiple processes', msg.lower())
        self.assertIn('python3', msg)
        self.assertIn('12345', msg)

    def test_busy_with_no_extra_holders_still_flags_host_side(self):
        color, msg = _classify(
            usb_info={'enumerated': True, 'usbtmc_loaded': False, 'lsof': []},
            visa_info={'error_class': 'busy'},
            disp_info={'cached_session': True},
        )
        self.assertEqual(color, 'red')
        self.assertIn('busy', msg.lower())

    def test_nodev_classifies_as_transient(self):
        color, msg = _classify(
            usb_info={'enumerated': True, 'usbtmc_loaded': False, 'lsof': []},
            visa_info={'error_class': 'nodev', 'error': '[Errno 19]'},
            disp_info={'cached_session': True},
        )
        self.assertEqual(color, 'yellow')
        self.assertIn('TRANSIENT', msg)
        self.assertIn('re-enumeration', msg)
        self.assertIn('auto-recover', msg)

    def test_timeout_classifies_as_instrument_wedged(self):
        """The post-JUL-7 flagship case: device enumerates fine, opens fine,
        but *IDN? times out — wedged firmware, needs mains power-cycle."""
        color, msg = _classify(
            usb_info={'enumerated': True, 'usbtmc_loaded': False, 'lsof': []},
            visa_info={'error_class': 'timeout', 'error': '[Errno 110]'},
            disp_info={'cached_session': True},
        )
        self.assertEqual(color, 'red')
        self.assertIn('INSTRUMENT WEDGED', msg)
        self.assertIn('mains-side power-cycle', msg)
        self.assertIn("can't fix this", msg.lower())

    def test_not_enumerated_calls_out_power_and_cable(self):
        color, msg = _classify(
            usb_info={'enumerated': False, 'usbtmc_loaded': False, 'lsof': []},
            visa_info={'error': 'device not found'},
            disp_info={'cached_session': False},
        )
        self.assertEqual(color, 'red')
        self.assertIn('NOT ENUMERATED', msg)
        self.assertIn('power', msg.lower())
        self.assertIn('cable', msg.lower())

    def test_healthy_when_idn_returned(self):
        color, msg = _classify(
            usb_info={'enumerated': True, 'usbtmc_loaded': False, 'lsof': []},
            visa_info={'idn': 'KEITHLEY INSTRUMENTS,MODEL 2281S-20-6,4518305,01.08b'},
            disp_info={'cached_session': True},
        )
        self.assertEqual(color, 'green')
        self.assertIn('HEALTHY', msg)
        self.assertIn('2281S', msg)

    def test_visa_skipped_with_dispatcher_active_is_healthy(self):
        """When the bare pyvisa probe is skipped (hw_service already has
        a shared session) and the dispatcher reports cached drivers, that
        means the path-of-real-use is alive — classify healthy."""
        color, msg = _classify(
            usb_info={'enumerated': True, 'usbtmc_loaded': False, 'lsof': []},
            visa_info={'skipped': True, 'reason': 'hw_service has a shared session'},
            disp_info={'cached_session': True,
                       'cached_drivers': [{'device_name': 'keithley_battery', 'driver_class': 'KeithleyBattery'}]},
        )
        self.assertEqual(color, 'green')
        self.assertIn('HEALTHY', msg)
        self.assertIn('shared session', msg.lower())

    def test_non_usb_tmc_instrument_gets_clear_message(self):
        """LabJack/Picoscope/Acroname all return characteristic VISA errors
        when probed with pyvisa-py; we surface a 'tool doesn't apply' message
        rather than the catch-all UNCLEAR."""
        for err in [
            'VI_ERROR_INV_RSRC_NAME (-1073807342): Invalid resource reference specified. Parsing error.',
            'No device found.',
        ]:
            color, msg = _classify(
                usb_info={'enumerated': True, 'usbtmc_loaded': False, 'lsof': []},
                visa_info={'error': err},
                disp_info={'cached_session': False},
            )
            self.assertEqual(color, 'yellow', f'wrong color for err={err!r}')
            self.assertIn('NOT USB-TMC', msg)
            self.assertIn('vendor SDK', msg)

    def test_unclear_fallback(self):
        """Everything else falls into 'unclear' — yellow, asks user to look."""
        color, msg = _classify(
            usb_info={'enumerated': True, 'usbtmc_loaded': False, 'lsof': []},
            visa_info={},  # no idn, no error_class
            disp_info={},
        )
        self.assertEqual(color, 'yellow')
        self.assertIn('UNCLEAR', msg)


if __name__ == '__main__':
    unittest.main()
