#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for gateway-auth token refresh behavior.

Pins down the refresh-storm fix: the refresh-ahead margin must scale with
the token's issued lifetime (a fixed 60s margin against 60s tokens made
EVERY request a refresh round-trip), a failed refresh must fall back to a
still-valid stored token instead of hard-failing the command, and the
single retry must only happen when the request never reached the server
(replaying a refresh can trip the server's rotation replay detection).
"""

import base64
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

import requests
from requests.cookies import RequestsCookieJar

from cli import gateway_auth

URL = 'https://auth.example.com'


def make_token(lifetime, expires_in):
    """Unsigned JWT-shaped token: iat/exp chosen so the token was issued
    with `lifetime` seconds total and expires `expires_in` seconds from
    now (negative = already expired)."""
    now = time.time()
    payload = {'iat': now + expires_in - lifetime, 'exp': now + expires_in,
               'email': 'user@example.com', 'type': 'access'}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'header.{body}.signature'


class FakeResponse:
    def __init__(self, status_code=200, body=None, cookies=None):
        self.status_code = status_code
        self._body = body or {}
        self.cookies = RequestsCookieJar()
        for k, v in (cookies or {}).items():
            self.cookies.set(k, v)

    def json(self):
        return self._body


class GatewayAuthRefreshTests(unittest.TestCase):

    def setUp(self):
        self.tmp = mock.patch.dict(os.environ, {
            'LAGER_GATEWAY_AUTH_FILE': os.path.join(
                os.environ.get('TMPDIR', '/tmp'),
                f'lager-gw-auth-test-{os.getpid()}-{id(self)}.json'),
        })
        self.tmp.start()
        self.addCleanup(self.tmp.stop)
        self.addCleanup(self._unlink_store)

    def _unlink_store(self):
        try:
            os.unlink(os.environ['LAGER_GATEWAY_AUTH_FILE'])
        except FileNotFoundError:
            pass

    def store_session(self, token, cookies=None):
        gateway_auth.save_login(URL, token, cookies or {'refresh_token': 'r1'})

    # -- margin scaling ----------------------------------------------------

    def test_fresh_short_ttl_token_is_returned_without_refresh(self):
        # 60s-lifetime token with 55s left: margin is 15s (lifetime/4), so
        # the cached token is still fresh. Before the fix the fixed 60s
        # margin forced a refresh here on EVERY call.
        self.store_session(make_token(lifetime=60, expires_in=55))
        with mock.patch.object(gateway_auth.requests, 'post') as post:
            post.side_effect = AssertionError('refresh must not be attempted')
            token = gateway_auth.access_token_for(URL)
        self.assertIsNotNone(token)

    def test_long_ttl_token_near_expiry_is_refreshed(self):
        # 15-minute token with 30s left: margin stays 60s, refresh happens.
        self.store_session(make_token(lifetime=900, expires_in=30))
        fresh = make_token(lifetime=900, expires_in=900)
        with mock.patch.object(gateway_auth.requests, 'post',
                               return_value=FakeResponse(body={'accessToken': fresh})) as post:
            token = gateway_auth.access_token_for(URL)
        self.assertEqual(token, fresh)
        self.assertEqual(post.call_count, 1)

    # -- failed-refresh fallback -------------------------------------------

    def test_failed_refresh_falls_back_to_unexpired_token(self):
        stored = make_token(lifetime=60, expires_in=10)  # inside margin, not expired
        self.store_session(stored)
        with mock.patch.object(gateway_auth.requests, 'post',
                               return_value=FakeResponse(status_code=503)):
            token = gateway_auth.access_token_for(URL)
        self.assertEqual(token, stored)

    def test_failed_refresh_with_expired_token_returns_none(self):
        self.store_session(make_token(lifetime=60, expires_in=-5))
        with mock.patch.object(gateway_auth.requests, 'post',
                               return_value=FakeResponse(status_code=503)):
            self.assertIsNone(gateway_auth.access_token_for(URL))

    # -- retry semantics ---------------------------------------------------

    def test_connection_error_is_retried_once(self):
        self.store_session(make_token(lifetime=60, expires_in=-5))
        fresh = make_token(lifetime=60, expires_in=60)
        with mock.patch.object(gateway_auth.requests, 'post') as post:
            post.side_effect = [requests.ConnectionError('down'),
                                FakeResponse(body={'accessToken': fresh})]
            token = gateway_auth.access_token_for(URL)
        self.assertEqual(token, fresh)
        self.assertEqual(post.call_count, 2)

    def test_read_timeout_is_not_retried(self):
        # A timeout is ambiguous — the server may have rotated the refresh
        # token already, and replaying would trip replay detection.
        self.store_session(make_token(lifetime=60, expires_in=-5))
        with mock.patch.object(gateway_auth.requests, 'post') as post:
            post.side_effect = requests.Timeout('slow')
            self.assertIsNone(gateway_auth.access_token_for(URL))
        self.assertEqual(post.call_count, 1)

    # -- rotation persistence ----------------------------------------------

    def test_rotated_cookies_and_token_are_persisted(self):
        self.store_session(make_token(lifetime=60, expires_in=-5),
                           cookies={'refresh_token': 'r1', 'csrf_token': 'c1'})
        fresh = make_token(lifetime=60, expires_in=60)
        rotated = FakeResponse(body={'accessToken': fresh},
                               cookies={'refresh_token': 'r2'})
        with mock.patch.object(gateway_auth.requests, 'post', return_value=rotated):
            token = gateway_auth.access_token_for(URL)
        self.assertEqual(token, fresh)
        entry = gateway_auth._load_store()['authServers'][URL]
        self.assertEqual(entry['accessToken'], fresh)
        self.assertEqual(entry['cookies']['refresh_token'], 'r2')
        self.assertEqual(entry['cookies']['csrf_token'], 'c1')  # unrotated cookie kept


if __name__ == '__main__':
    unittest.main()
