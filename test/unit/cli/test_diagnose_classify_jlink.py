#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `lager diagnose`'s J-Link classification — the decision tree
that turns the `/diagnose/usb` + `/diagnose/jlink` payloads into a one-line,
user-actionable diagnosis for a debug net. Pins down each branch so a future
refactor can't silently demote (say) "TARGET UNPOWERED" to "UNCLEAR".

Sibling of test_diagnose_classify.py, which covers the USB-TMC path.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from cli.commands.box.diagnose import _classify_jlink


def _jlink(**over):
    """A baseline 'everything healthy up to the connect probe' jlink payload,
    overridden per-test. Defaults: software installed, probe enumerated +
    visible, no gdbserver running."""
    base = {
        'mode': 'jlink',
        'backend': 'jlink',
        'jlink_installed': True,
        'probe_enumerated': True,
        'probe_visible': True,
        'holders': [],
        'gdbserver': {'running': False},
    }
    base.update(over)
    return base


class ClassifyJlinkTests(unittest.TestCase):

    def test_endpoint_unavailable_points_at_deploy(self):
        """Old box without the /diagnose/jlink route — tell the user to deploy."""
        color, msg = _classify_jlink({}, {'unavailable': 'pre-deploy image'})
        self.assertEqual(color, 'yellow')
        self.assertIn('lager update', msg)

    def test_transport_error_is_red(self):
        color, msg = _classify_jlink({}, {'transport_error': 'Connection refused'})
        self.assertEqual(color, 'red')
        self.assertIn('Connection refused', msg)

    def test_software_missing_wins_first(self):
        """No J-Link tools on the box is the root cause regardless of USB state."""
        color, msg = _classify_jlink({}, _jlink(jlink_installed=False))
        self.assertEqual(color, 'red')
        self.assertIn('SOFTWARE MISSING', msg)

    def test_probe_not_enumerated(self):
        color, msg = _classify_jlink({}, _jlink(probe_enumerated=False))
        self.assertEqual(color, 'red')
        self.assertIn('NOT ON USB', msg)

    def test_probe_claimed_names_the_holder(self):
        """Enumerated but invisible to JLinkExe, with a process holding it →
        call out the racing process (usually a stale gdbserver)."""
        color, msg = _classify_jlink(
            {},
            _jlink(probe_visible=False,
                   holders=[{'command': 'JLinkGDBServer', 'pid': '4242'}]),
        )
        self.assertEqual(color, 'red')
        self.assertIn('PROBE CLAIMED', msg)
        self.assertIn('JLinkGDBServer(4242)', msg)

    def test_probe_claimed_falls_back_to_usb_lsof(self):
        """Holder may be reported by the USB endpoint's lsof rather than the
        J-Link endpoint — still attribute the claim."""
        color, msg = _classify_jlink(
            {'lsof': [{'command': 'python3', 'pid': '99'}]},
            _jlink(probe_visible=False, holders=[]),
        )
        self.assertEqual(color, 'red')
        self.assertIn('PROBE CLAIMED', msg)
        self.assertIn('python3(99)', msg)

    def test_probe_wedged_when_invisible_and_unheld(self):
        """Enumerated, nobody holding it, but JLinkExe still can't see it →
        the probe firmware is wedged; power-cycle it."""
        color, msg = _classify_jlink({}, _jlink(probe_visible=False, holders=[]))
        self.assertEqual(color, 'red')
        self.assertIn('PROBE WEDGED', msg)

    def test_gdbserver_running_and_healthy(self):
        color, msg = _classify_jlink(
            {}, _jlink(gdbserver={'running': True, 'pid': 7, 'logfile_ok': True}))
        self.assertEqual(color, 'green')
        self.assertIn('HEALTHY', msg)

    def test_gdbserver_running_but_log_failed(self):
        color, msg = _classify_jlink(
            {}, _jlink(gdbserver={'running': True, 'pid': 7, 'logfile_ok': False}))
        self.assertEqual(color, 'red')
        self.assertIn('GDBSERVER WEDGED', msg)

    def test_connect_skipped_is_inconclusive(self):
        color, msg = _classify_jlink(
            {}, _jlink(connect_skipped=True, connect_skip_reason='idle but visible'))
        self.assertEqual(color, 'yellow')
        self.assertIn('INCONCLUSIVE', msg)

    def test_connect_ok_is_healthy_with_vtref_and_core(self):
        color, msg = _classify_jlink(
            {},
            _jlink(device='NRF5340_XXAA_APP',
                   connect={'connect_ok': True, 'connect_error_class': 'ok',
                            'vtref_mv': 3300, 'core': 'Cortex-M33'}))
        self.assertEqual(color, 'green')
        self.assertIn('HEALTHY', msg)
        self.assertIn('3.300V', msg)
        self.assertIn('Cortex-M33', msg)

    def test_target_unpowered_reports_vtref(self):
        color, msg = _classify_jlink(
            {},
            _jlink(device='X',
                   connect={'connect_error_class': 'no_target_power', 'vtref_mv': 0}))
        self.assertEqual(color, 'red')
        self.assertIn('TARGET UNPOWERED', msg)
        self.assertIn('0.000V', msg)

    def test_target_locked(self):
        color, msg = _classify_jlink(
            {}, _jlink(device='X', connect={'connect_error_class': 'locked', 'vtref_mv': 3300}))
        self.assertEqual(color, 'red')
        self.assertIn('TARGET LOCKED', msg)

    def test_wrong_device_is_yellow_and_names_device(self):
        color, msg = _classify_jlink(
            {}, _jlink(device='BOGUS_MCU', connect={'connect_error_class': 'wrong_device'}))
        self.assertEqual(color, 'yellow')
        self.assertIn('DEVICE NAME', msg)
        self.assertIn('BOGUS_MCU', msg)

    def test_no_target_comms(self):
        color, msg = _classify_jlink(
            {},
            _jlink(device='X',
                   connect={'connect_error_class': 'no_target_comms', 'vtref_mv': 3300}))
        self.assertEqual(color, 'red')
        self.assertIn('NO TARGET COMMS', msg)

    def test_openocd_basic_enumerated(self):
        color, msg = _classify_jlink(
            {},
            {'mode': 'openocd-basic', 'backend': 'openocd',
             'probe_enumerated': True, 'openocd_gdbserver': {'running': False}})
        self.assertEqual(color, 'yellow')
        self.assertIn('OPENOCD', msg)

    def test_openocd_basic_not_enumerated(self):
        color, msg = _classify_jlink(
            {}, {'mode': 'openocd-basic', 'backend': 'openocd', 'probe_enumerated': False})
        self.assertEqual(color, 'red')
        self.assertIn('NOT ON USB', msg)


if __name__ == '__main__':
    unittest.main()
