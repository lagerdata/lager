#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `cli.context.error_handlers.map_system_error` /
`format_system_error_for_user` — the 0.20.0 helper that turns raw
[Errno 16/19/110] / libusb / pyvisa errors into actionable text.

Pins:
  - Each of the three errnos maps to its own headline + actions.
  - Detection works both via explicit `[Errno N]` and via substring fallback.
  - Unrelated errors return None (caller falls back to raw).
  - LAGER_DEBUG=1 includes the raw error in the formatted output.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from cli.context.error_handlers import map_system_error, format_system_error_for_user


class MapSystemErrorTests(unittest.TestCase):

    def test_errno_16_maps_to_busy_message(self):
        result = map_system_error('[Errno 16] Resource busy')
        self.assertIsNotNone(result)
        headline, actions = result
        self.assertIn('USB device busy', headline)
        self.assertTrue(any('lager diagnose' in a for a in actions))

    def test_errno_19_maps_to_nodev_message(self):
        result = map_system_error('[Errno 19] No such device (it may have been disconnected)')
        self.assertIsNotNone(result)
        headline, actions = result
        self.assertIn('disappeared from USB', headline)
        self.assertTrue(any('docker restart lager' in a for a in actions))

    def test_errno_110_maps_to_timeout_message(self):
        result = map_system_error('[Errno 110] Operation timed out')
        self.assertIsNotNone(result)
        headline, actions = result
        self.assertIn('wedged', headline)
        self.assertTrue(any('power-cycle' in a for a in actions))

    def test_unrelated_error_returns_none(self):
        for s in [
            'Some random error',
            '[Errno 5] I/O error',
            'NotImplementedError: foo',
            '',
            None,
        ]:
            self.assertIsNone(map_system_error(s), f'unexpected mapping for {s!r}')

    def test_substring_fallback_busy(self):
        """Errors that don't have an explicit `[Errno 16]` but contain
        'Resource busy' should still map (e.g. wrapped libusb errors)."""
        result = map_system_error('usb.core.USBError: Resource busy at endpoint')
        self.assertIsNotNone(result)
        self.assertIn('USB device busy', result[0])

    def test_substring_fallback_nodev(self):
        result = map_system_error('ValueError: No such device (it may have been disconnected)')
        self.assertIsNotNone(result)
        self.assertIn('disappeared', result[0])

    def test_substring_fallback_timeout(self):
        result = map_system_error('Timed out waiting for SCPI response')
        self.assertIsNotNone(result)
        self.assertIn('wedged', result[0])

    def test_format_returns_styled_multiline(self):
        out = format_system_error_for_user('[Errno 16] Resource busy')
        self.assertIsNotNone(out)
        # Multi-line: headline + at least one action line.
        self.assertGreaterEqual(out.count('\n'), 1)
        # ANSI escape from click.style on the headline.
        self.assertIn('\x1b[', out)

    def test_format_returns_none_for_unmapped(self):
        self.assertIsNone(format_system_error_for_user('garbage error'))
        self.assertIsNone(format_system_error_for_user(''))

    def test_format_includes_raw_when_LAGER_DEBUG_set(self):
        raw = '[Errno 19] No such device blah blah'
        with patch.dict(os.environ, {'LAGER_DEBUG': '1'}):
            out = format_system_error_for_user(raw)
        self.assertIn('--- raw error ---', out)
        self.assertIn(raw, out)

    def test_format_omits_raw_without_LAGER_DEBUG(self):
        raw = '[Errno 19] No such device blah blah'
        env = {k: v for k, v in os.environ.items() if k != 'LAGER_DEBUG'}
        with patch.dict(os.environ, env, clear=True):
            out = format_system_error_for_user(raw)
        self.assertNotIn('--- raw error ---', out)


if __name__ == '__main__':
    unittest.main()
