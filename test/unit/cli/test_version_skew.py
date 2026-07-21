#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `cli/core/version_skew.check_and_warn` — the one-line
warning that fires when the CLI is a minor version ahead of the box.

Pins:
  - Warns when CLI minor > box minor (same major).
  - Does NOT warn for equal versions or box-ahead.
  - Per-process caches by IP (second call is a no-op).
  - Fails open: any fetch/parse/import error silently skips.
"""

import io
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from cli.core import version_skew


class VersionSkewTests(unittest.TestCase):

    def setUp(self):
        version_skew.reset_cache_for_tests()
        self.stderr_buf = io.StringIO()
        self._stderr_patch = patch.object(sys, 'stderr', self.stderr_buf)
        self._stderr_patch.start()

    def tearDown(self):
        self._stderr_patch.stop()

    def _mock_box(self, version):
        """Return a side_effect that has requests.get() return a fake 200
        shaped like GET :9000/status."""
        def _fake_get(url, timeout=None, headers=None):
            assert ':9000/status' in url, f'expected :9000/status, got {url}'
            return MagicMock(status_code=200, json=lambda: {'healthy': True, 'version': version})
        return _fake_get

    def test_warns_when_cli_minor_ahead(self):
        with patch('cli.core.version_skew.requests.get', side_effect=self._mock_box('0.18.5')), \
             patch('cli.__version__', '0.20.0'):
            version_skew.check_and_warn('10.0.0.1', 'test-box')
        out = self.stderr_buf.getvalue()
        self.assertIn('Box test-box is on lager 0.18.5', out)
        self.assertIn('CLI is on 0.20.0', out)
        self.assertIn('lager box update --box test-box', out)

    def test_no_warning_when_versions_match(self):
        with patch('cli.core.version_skew.requests.get', side_effect=self._mock_box('0.20.0')), \
             patch('cli.__version__', '0.20.0'):
            version_skew.check_and_warn('10.0.0.2', 'test-box')
        self.assertEqual(self.stderr_buf.getvalue(), '')

    def test_no_warning_when_box_is_ahead(self):
        with patch('cli.core.version_skew.requests.get', side_effect=self._mock_box('0.21.0')), \
             patch('cli.__version__', '0.20.0'):
            version_skew.check_and_warn('10.0.0.3', 'test-box')
        self.assertEqual(self.stderr_buf.getvalue(), '')

    def test_second_call_is_cached(self):
        with patch('cli.core.version_skew.requests.get', side_effect=self._mock_box('0.18.5')) as mock_get, \
             patch('cli.__version__', '0.20.0'):
            version_skew.check_and_warn('10.0.0.4', 'test-box')
            version_skew.check_and_warn('10.0.0.4', 'test-box')
            version_skew.check_and_warn('10.0.0.4', 'test-box')
        # Only one HTTP call regardless of how many CLI commands hit it.
        self.assertEqual(mock_get.call_count, 1)

    def test_different_box_ips_each_check_once(self):
        with patch('cli.core.version_skew.requests.get', side_effect=self._mock_box('0.18.5')) as mock_get, \
             patch('cli.__version__', '0.20.0'):
            version_skew.check_and_warn('10.0.0.5', 'A')
            version_skew.check_and_warn('10.0.0.6', 'B')
        self.assertEqual(mock_get.call_count, 2)

    def test_fails_open_on_network_error(self):
        import requests
        with patch('cli.core.version_skew.requests.get',
                   side_effect=requests.exceptions.ConnectTimeout()), \
             patch('cli.__version__', '0.20.0'):
            # Must not raise.
            version_skew.check_and_warn('10.0.0.7', 'test-box')
        self.assertEqual(self.stderr_buf.getvalue(), '')

    def test_fails_open_on_unparseable_box_version(self):
        with patch('cli.core.version_skew.requests.get',
                   side_effect=self._mock_box('not-a-version')), \
             patch('cli.__version__', '0.20.0'):
            version_skew.check_and_warn('10.0.0.8', 'test-box')
        self.assertEqual(self.stderr_buf.getvalue(), '')

    def test_uses_ip_in_message_when_name_missing(self):
        with patch('cli.core.version_skew.requests.get', side_effect=self._mock_box('0.18.5')), \
             patch('cli.__version__', '0.20.0'):
            version_skew.check_and_warn('10.0.0.9', None)
        self.assertIn('Box 10.0.0.9 is on', self.stderr_buf.getvalue())

    def test_warns_when_status_route_missing(self):
        """404 from :9000/status = box image predates the :9000 API → warn."""
        with patch('cli.core.version_skew.requests.get',
                   return_value=MagicMock(status_code=404)), \
             patch('cli.__version__', '0.20.0'):
            version_skew.check_and_warn('10.0.0.10', 'old-box')
        out = self.stderr_buf.getvalue()
        self.assertIn('old-box', out)
        self.assertIn('lager box update --box old-box', out)

    def test_silent_on_other_http_errors(self):
        """A 500 (box present but unhealthy) still fails open silently."""
        with patch('cli.core.version_skew.requests.get',
                   return_value=MagicMock(status_code=500)), \
             patch('cli.__version__', '0.20.0'):
            version_skew.check_and_warn('10.0.0.11', 'test-box')
        self.assertEqual(self.stderr_buf.getvalue(), '')

    def test_parse_minor_helper(self):
        self.assertEqual(version_skew._parse_minor('0.20.0'), (0, 20))
        self.assertEqual(version_skew._parse_minor('v1.2.3'), (1, 2))
        self.assertEqual(version_skew._parse_minor('0.20'), (0, 20))
        self.assertEqual(version_skew._parse_minor('1.2.3-rc4'), (1, 2))
        self.assertIsNone(version_skew._parse_minor(''))
        self.assertIsNone(version_skew._parse_minor('garbage'))


if __name__ == '__main__':
    unittest.main()
