# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the /authorize-key fixed-window rate limiter in
box/lager/http_handlers/ssh_handler.py: per-IP window counting, reset on
success, expired-entry pruning, and the hard cap that keeps a many-unique-IP
flood from turning the prune scan into per-request O(N) work under the lock.

ssh_handler.py is loaded directly via importlib so this test doesn't pull in
the full box-side lager package.
"""

import importlib.util
import os
import unittest
from unittest import mock


HERE = os.path.dirname(__file__)
HANDLER_PATH = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'http_handlers', 'ssh_handler.py')
)


def _load_handler():
    spec = importlib.util.spec_from_file_location('ssh_handler_rate_limit_mod', HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


handler = _load_handler()


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        handler._rate_limit_attempts.clear()
        self.now = 1000.0
        patcher = mock.patch.object(handler.time, 'monotonic', side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(handler._rate_limit_attempts.clear)

    def test_limit_trips_after_max_attempts(self):
        for _ in range(handler._RATE_LIMIT_MAX_ATTEMPTS):
            self.assertFalse(handler._rate_limited('10.0.0.1'))
        self.assertTrue(handler._rate_limited('10.0.0.1'))

    def test_reset_clears_window(self):
        for _ in range(handler._RATE_LIMIT_MAX_ATTEMPTS + 1):
            handler._rate_limited('10.0.0.1')
        handler._rate_limit_reset('10.0.0.1')
        self.assertFalse(handler._rate_limited('10.0.0.1'))

    def test_window_expiry_resets_count(self):
        for _ in range(handler._RATE_LIMIT_MAX_ATTEMPTS + 1):
            handler._rate_limited('10.0.0.1')
        self.now += handler._RATE_LIMIT_WINDOW_SECONDS
        self.assertFalse(handler._rate_limited('10.0.0.1'))

    def test_prune_evicts_expired_entries(self):
        for i in range(handler._RATE_LIMIT_PRUNE_THRESHOLD + 1):
            handler._rate_limited(f'10.0.{i // 256}.{i % 256}')
        self.now += handler._RATE_LIMIT_WINDOW_SECONDS
        handler._rate_limited('10.99.99.99')  # triggers the prune
        self.assertLessEqual(len(handler._rate_limit_attempts), 2)

    def test_hard_cap_clears_under_live_flood(self):
        # All entries stay inside their window (a live distributed flood), so
        # pruning removes nothing; past 2x threshold the dict must be cleared
        # rather than scanned forever.
        for i in range(2 * handler._RATE_LIMIT_PRUNE_THRESHOLD + 2):
            handler._rate_limited(f'10.{i // 65536}.{(i // 256) % 256}.{i % 256}')
        self.assertLessEqual(
            len(handler._rate_limit_attempts),
            2 * handler._RATE_LIMIT_PRUNE_THRESHOLD,
        )
        # Limiting still functions after the clear.
        for _ in range(handler._RATE_LIMIT_MAX_ATTEMPTS):
            self.assertFalse(handler._rate_limited('10.0.0.1'))
        self.assertTrue(handler._rate_limited('10.0.0.1'))


if __name__ == '__main__':
    unittest.main()
