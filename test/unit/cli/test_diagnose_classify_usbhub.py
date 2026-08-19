#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `lager diagnose`'s USB-hub classification — the decision tree
that turns a `/diagnose/usbhub` payload into a one-line, user-actionable
diagnosis.

The branch that matters most is HUB WEDGED: enumerated in sysfs, held by
nobody, and still invisible to the vendor SDK. sysfs and lsof both report a
perfectly healthy device in that state, which is why a bench spent 42 hours
looking like flaky software (issue #196). If a refactor ever demotes that to
UNCLEAR, this file is what catches it.

Sibling of test_diagnose_classify.py (USB-TMC) and
test_diagnose_classify_jlink.py (debug nets).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from cli.commands.box.diagnose import _classify_usb_hub, _fmt_usbhub_lines


def _hub(**over):
    """A healthy hub payload, overridden per-test."""
    d = {
        'enumerated': True,
        'serial_visible_to_sdk': True,
        'hub_opens': True,
        'holders': [],
        'devnum': 64,
        'devnum_min': 60,
        'devnum_max': 70,
        'devnum_median': 65,
    }
    d.update(over)
    return d


class ClassifyUsbHubTests(unittest.TestCase):
    def test_a_healthy_hub_is_reachable(self):
        color, headline = _classify_usb_hub(_hub())
        self.assertEqual('green', color)
        self.assertIn('REACHABLE', headline)

    def test_reachable_does_not_overclaim(self):
        """It tested the path to the hub, not whatever is behind its ports."""
        _, headline = _classify_usb_hub(_hub())
        self.assertIn('not the', headline)

    def test_not_enumerated_points_at_the_cable(self):
        color, headline = _classify_usb_hub(_hub(enumerated=False))
        self.assertEqual('red', color)
        self.assertIn('NOT ENUMERATED', headline)

    def test_enumerated_but_invisible_to_the_sdk_is_the_wedge(self):
        """THE case this endpoint exists for."""
        color, headline = _classify_usb_hub(
            _hub(serial_visible_to_sdk=False, hub_opens=False))
        self.assertEqual('red', color)
        self.assertIn('HUB WEDGED', headline)

    def test_the_wedge_says_a_service_restart_will_not_help(self):
        """Measured: the box restarted itself twice for this and a fresh
        process failed identically. Saying so is the point."""
        _, headline = _classify_usb_hub(
            _hub(serial_visible_to_sdk=False, hub_opens=False))
        self.assertIn('restarting box services will not help', headline.lower())

    def test_the_wedge_mentions_churn_when_the_devnum_is_an_outlier(self):
        _, headline = _classify_usb_hub(
            _hub(serial_visible_to_sdk=False, hub_opens=False,
                 devnum=93, devnum_min=60, devnum_max=93, devnum_median=65))
        self.assertIn('93', headline)
        self.assertIn('re-enumerating', headline)

    def test_the_wedge_omits_churn_when_the_devnum_is_ordinary(self):
        """A hub that is wedged without having re-enumerated is a different
        story, and inventing churn evidence would send the reader hunting for
        a cause that is not there."""
        _, headline = _classify_usb_hub(
            _hub(serial_visible_to_sdk=False, hub_opens=False))
        self.assertNotIn('re-enumerating', headline)

    def test_an_unsupported_vendor_is_not_reported_as_busy(self):
        """Caught on hardware. A Plugable hub has no BrainStem driver to probe
        with, which is permanent -- but reusing `probe_skipped` for it made
        diagnose print "BUSY ... rerun when it is idle", sending someone to
        wait for a condition that can never change. It must be its own state.
        """
        color, headline = _classify_usb_hub(_hub(
            hub_diagnostics_supported=False,
            hub_diagnostics_skip_reason=(
                'hub diagnostics are implemented for Acroname hubs only, and '
                'vendor 2230 is not one'),
            serial_visible_to_sdk=None, hub_opens=None))
        self.assertEqual('yellow', color)
        self.assertIn('NOT SUPPORTED', headline)
        self.assertNotIn('BUSY', headline)
        self.assertNotIn('rerun when it is idle', headline)
        self.assertIn('not a fault', headline)

    def test_an_unsupported_vendor_outranks_the_sdk_wedge(self):
        # serial_visible_to_sdk is None (never scanned), not False. A hub that
        # was never probed must not be reported as invisible to the SDK.
        _, headline = _classify_usb_hub(_hub(
            hub_diagnostics_supported=False,
            hub_diagnostics_skip_reason='vendor 2230 is not Acroname',
            serial_visible_to_sdk=None, hub_opens=None))
        self.assertNotIn('WEDGED', headline)

    def test_an_older_box_omitting_the_flag_still_classifies(self):
        # `hub_diagnostics_supported` is absent from an older box's payload;
        # `.get()` returns None and must not trip the new branch.
        color, headline = _classify_usb_hub(_hub())
        self.assertEqual('green', color)
        self.assertIn('REACHABLE', headline)

    def test_the_rendered_line_says_n_a_not_skipped(self):
        lines = _fmt_usbhub_lines(_hub(
            hub_diagnostics_supported=False,
            hub_diagnostics_skip_reason='vendor 2230 is not Acroname',
            hub_opens=None))
        hub_open = [l for l in lines if l.startswith('hub open:')]
        self.assertEqual(len(hub_open), 1)
        self.assertIn('n/a', hub_open[0])
        self.assertNotIn('skipped', hub_open[0])

    def test_a_busy_hub_is_reported_not_guessed_at(self):
        color, headline = _classify_usb_hub(
            _hub(probe_skipped=True, probe_skip_reason='hub is busy: locked',
                 hub_opens=None))
        self.assertEqual('yellow', color)
        self.assertIn('BUSY', headline)

    def test_a_held_hub_names_who_holds_it(self):
        color, headline = _classify_usb_hub(
            _hub(hub_opens=False, holders=[{'pid': 4321, 'command': 'python3'}]))
        self.assertEqual('yellow', color)
        self.assertIn('HUB CLAIMED', headline)
        self.assertIn('4321', headline)

    def test_an_open_failure_with_no_holder_is_red(self):
        color, headline = _classify_usb_hub(
            _hub(hub_opens=False, hub_open_error='DeviceNotFoundError: nope'))
        self.assertEqual('red', color)
        self.assertIn('WILL NOT OPEN', headline)

    def test_an_older_box_without_the_endpoint_says_so(self):
        color, headline = _classify_usb_hub(
            {'unavailable': 'endpoint not on this box (pre-0.20 image)'})
        self.assertEqual('yellow', color)
        self.assertIn('Update the box', headline)

    def test_ordering_puts_not_enumerated_ahead_of_the_wedge(self):
        """A hub that is not on the bus at all cannot also be 'wedged'; the
        more specific fact has to win or the remedy is wrong."""
        _, headline = _classify_usb_hub(
            _hub(enumerated=False, serial_visible_to_sdk=False))
        self.assertIn('NOT ENUMERATED', headline)


class FormatUsbHubTests(unittest.TestCase):
    def test_an_outlier_devnum_is_called_out_inline(self):
        lines = '\n'.join(_fmt_usbhub_lines(
            _hub(devnum=93, devnum_min=60, devnum_max=93, devnum_median=65)))
        self.assertIn('far above its peers', lines)

    def test_an_ordinary_devnum_is_not(self):
        lines = '\n'.join(_fmt_usbhub_lines(_hub()))
        self.assertNotIn('far above its peers', lines)

    def test_missing_devnum_context_does_not_crash(self):
        """An older box, or a device sysfs could not read, sends None here."""
        lines = '\n'.join(_fmt_usbhub_lines(
            {'enumerated': True, 'devnum': None, 'devnum_min': None,
             'devnum_max': None, 'devnum_median': None}))
        self.assertIn('devnum:', lines)

    def test_the_bus_listing_shows_every_vendor_device(self):
        """One physical hub is several USB devices; the one with an empty
        serial is often the one proving it is physically present."""
        lines = '\n'.join(_fmt_usbhub_lines(_hub(vendor_devices=[
            {'sysfs_name': '1-5', 'vid': '24ff', 'pid': '8011',
             'serial': None, 'devnum': '92', 'product': None},
            {'sysfs_name': '1-5.1', 'vid': '24ff', 'pid': '0011',
             'serial': 'E6BACCD5', 'devnum': '93', 'product': 'USBHub2x4 BS'},
        ])))
        self.assertIn('24ff:8011', lines)
        self.assertIn('24ff:0011', lines)
        self.assertIn('(none)', lines)

    def test_an_sdk_scan_that_saw_nothing_says_nothing_not_blank(self):
        lines = '\n'.join(_fmt_usbhub_lines(
            _hub(serial_visible_to_sdk=False, sdk_scan_serials=[])))
        self.assertIn('(nothing)', lines)


if __name__ == '__main__':
    unittest.main()
