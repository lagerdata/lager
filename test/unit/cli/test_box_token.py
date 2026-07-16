# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for cli/box_token.py: how the CLI gets a bearer token for a box that
asks for one.

The behavior that matters most: a box that has not been configured to require a
token -- which is every box by default -- produces no token and no error, so the
CLI is unchanged for everyone who never set this up. The rest is making sure a
misbehaving helper (missing, slow, failing, silent) degrades to "no token"
rather than to a crash or a hang.
"""

import base64
import json
import time
import unittest
from unittest import mock

from cli import box_token


def jwt_with_exp(exp):
    payload = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).decode().rstrip('=')
    return f'header.{payload}.signature'


class TokenResolutionTests(unittest.TestCase):
    def setUp(self):
        box_token.clear_token_cache()
        # Start every test from a clean environment.
        self._env = mock.patch.dict('os.environ', {}, clear=False)
        self._env.start()
        for key in ('LAGER_TOKEN', 'LAGER_TOKEN_HELPER'):
            import os
            os.environ.pop(key, None)

    def tearDown(self):
        self._env.stop()
        box_token.clear_token_cache()

    def test_nothing_configured_is_a_no_op(self):
        with mock.patch.object(box_token, 'get_token_helper', return_value=None):
            self.assertIsNone(box_token.resolve_token('10.0.0.1'))

    def test_lager_token_env_wins(self):
        import os
        os.environ['LAGER_TOKEN'] = 'explicit-token'
        # Even a configured helper must not override an explicit token.
        with mock.patch.object(box_token, 'get_token_helper', return_value='/bin/echo other'):
            self.assertEqual(box_token.resolve_token('10.0.0.1'), 'explicit-token')

    def test_helper_stdout_becomes_the_token(self):
        token = jwt_with_exp(int(time.time()) + 600)
        with mock.patch.object(box_token, 'get_token_helper', return_value='/bin/echo ' + token):
            self.assertEqual(box_token.resolve_token('10.0.0.1'), token)

    def test_helper_is_told_which_box(self):
        with mock.patch.object(
            box_token, 'get_token_helper',
            return_value="/bin/sh -c 'printf %s \"$LAGER_BOX\"'",
        ):
            self.assertEqual(box_token.resolve_token('100.64.0.1'), '100.64.0.1')

    def test_failing_helper_yields_no_token(self):
        with mock.patch.object(box_token, 'get_token_helper', return_value='/usr/bin/false'):
            self.assertIsNone(box_token.resolve_token('10.0.0.1'))

    def test_missing_helper_yields_no_token(self):
        with mock.patch.object(box_token, 'get_token_helper',
                               return_value='/nonexistent/path/helper'):
            self.assertIsNone(box_token.resolve_token('10.0.0.1'))

    def test_silent_helper_yields_no_token(self):
        with mock.patch.object(box_token, 'get_token_helper', return_value='/usr/bin/true'):
            self.assertIsNone(box_token.resolve_token('10.0.0.1'))

    def test_helper_that_hangs_times_out(self):
        with mock.patch.object(box_token, '_HELPER_TIMEOUT_SECONDS', 1), \
             mock.patch.object(box_token, 'get_token_helper',
                               return_value="/bin/sh -c 'sleep 30'"):
            start = time.time()
            self.assertIsNone(box_token.resolve_token('10.0.0.1'))
            self.assertLess(time.time() - start, 5)

    def test_opaque_non_jwt_token_is_accepted(self):
        # Nothing here requires the token to be a JWT; only the box parses it.
        with mock.patch.object(box_token, 'get_token_helper',
                               return_value='/bin/echo opaque-token'):
            self.assertEqual(box_token.resolve_token('10.0.0.1'), 'opaque-token')

    def test_result_is_cached(self):
        token = jwt_with_exp(int(time.time()) + 600)
        real_run = box_token.subprocess.run
        calls = []

        def counting_run(*args, **kwargs):
            calls.append(args)
            return real_run(*args, **kwargs)

        with mock.patch.object(box_token, 'get_token_helper', return_value='/bin/echo ' + token), \
             mock.patch.object(box_token.subprocess, 'run', side_effect=counting_run):
            box_token.resolve_token('10.0.0.1')
            box_token.resolve_token('10.0.0.1')
        self.assertEqual(len(calls), 1)

    def test_expired_cache_refetches(self):
        token = jwt_with_exp(int(time.time()) + 600)
        box_token._cache['10.0.0.1'] = ('stale', time.time() - 1)
        with mock.patch.object(box_token, 'get_token_helper', return_value='/bin/echo ' + token):
            self.assertEqual(box_token.resolve_token('10.0.0.1'), token)


class AuthHookTests(unittest.TestCase):
    def setUp(self):
        box_token.clear_token_cache()

    def tearDown(self):
        box_token.clear_token_cache()

    class _FakeRequest:
        def __init__(self):
            self.headers = {}

    def test_hook_attaches_bearer_when_a_token_exists(self):
        with mock.patch.object(box_token, 'resolve_token', return_value='tok-123'):
            request = box_token.BoxTokenAuth('10.0.0.1')(self._FakeRequest())
        self.assertEqual(request.headers['Authorization'], 'Bearer tok-123')

    def test_hook_adds_nothing_when_unconfigured(self):
        with mock.patch.object(box_token, 'resolve_token', return_value=None):
            request = box_token.BoxTokenAuth('10.0.0.1')(self._FakeRequest())
        self.assertNotIn('Authorization', request.headers)


if __name__ == '__main__':
    unittest.main()
