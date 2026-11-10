#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager whoami` checks an expired session by renewing it.

It used to call any session with a stored refresh cookie "active (renews
automatically)". The cookie carries no expiry, and the server can end the
session at any time, so a session the server had already refused still read
as active while every gated box asked the user to sign in.

These tests drive `gateway_auth.check_session` and the command against a
fake auth server (`requests.post` is patched; nothing touches the network)
and a store in a temporary file. They pin down that:

- an unexpired token is active without any round trip;
- an expired token is renewed through `access_token_for`, once, and the
  rotated session is saved;
- 401 and 403 mean expired, with `lager login <url>`;
- no answer, a 5xx, a 429 or a 200 without a token mean "could not check";
- the failure memo keeps its reason, so a second check in the same process
  gives the same answer without a second round trip.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from importlib import import_module
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

import requests
from click.testing import CliRunner
from requests.cookies import RequestsCookieJar

from cli import gateway_auth

login_mod = import_module('cli.commands.utility.login')

URL = 'https://auth.example.com'
COOKIES = {'refresh_token': 'old', 'csrf': 'c'}


def make_token(expires_in, lifetime=900):
    now = time.time()
    payload = {'iat': now + expires_in - lifetime, 'exp': now + expires_in,
               'email': 'user@example.com'}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'header.{body}.signature'


class FakeResponse:
    def __init__(self, status_code=200, body=None, cookies=None):
        self.status_code = status_code
        self._body = body
        self.cookies = RequestsCookieJar()
        for k, v in (cookies or {}).items():
            self.cookies.set(k, v)

    def json(self):
        if self._body is None:
            raise ValueError('no JSON')
        return self._body


class SessionCheckTestCase(unittest.TestCase):
    """A store unique to each test, so the failure memo never leaks across."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {'LAGER_GATEWAY_AUTH_FILE': os.path.join(self.tmp, 'auth.json')}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(gateway_auth.PINNED_TOKEN_ENV, None)

    def store(self, expires_in, cookies=COOKIES):
        gateway_auth.save_login(URL, make_token(expires_in), cookies)

    def server(self, *answers):
        """Patch `requests.post` to give these answers in order.

        An answer is a FakeResponse or an exception instance to raise.
        """
        answers = list(answers)

        def post(*_args, **_kwargs):
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        patcher = mock.patch.object(gateway_auth.requests, 'post', side_effect=post)
        fake = patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class CheckSessionTests(SessionCheckTestCase):

    def test_unexpired_token_is_active_without_a_round_trip(self):
        self.store(expires_in=600)
        post = self.server()
        self.assertEqual(gateway_auth.check_session(URL), gateway_auth.SESSION_ACTIVE)
        post.assert_not_called()

    def test_expired_token_renewed_is_active_and_the_rotation_is_saved(self):
        self.store(expires_in=-600)
        post = self.server(FakeResponse(200, {'accessToken': make_token(900)},
                                        cookies={'refresh_token': 'new'}))
        self.assertEqual(gateway_auth.check_session(URL), gateway_auth.SESSION_ACTIVE)
        self.assertEqual(post.call_count, 1)
        saved = gateway_auth._load_store()['authServers'][URL]
        self.assertEqual(saved['cookies']['refresh_token'], 'new')
        self.assertEqual(saved['cookies']['csrf'], 'c')

    def test_401_is_expired(self):
        self.store(expires_in=-600)
        self.server(FakeResponse(401, {'message': 'Invalid or expired refresh token'}))
        self.assertEqual(gateway_auth.check_session(URL), gateway_auth.SESSION_EXPIRED)

    def test_403_is_expired(self):
        self.store(expires_in=-600)
        self.server(FakeResponse(403, {}))
        self.assertEqual(gateway_auth.check_session(URL), gateway_auth.SESSION_EXPIRED)

    def test_rejection_keeps_the_stored_session(self):
        """Contract §3.3: a failed refresh must not clear the session."""
        self.store(expires_in=-600)
        self.server(FakeResponse(401, {}))
        gateway_auth.check_session(URL)
        self.assertIn(URL, gateway_auth._load_store()['authServers'])

    def test_no_refresh_cookie_is_expired_without_a_round_trip(self):
        self.store(expires_in=-600, cookies={})
        post = self.server()
        self.assertEqual(gateway_auth.check_session(URL), gateway_auth.SESSION_EXPIRED)
        post.assert_not_called()

    def test_no_stored_session_is_expired(self):
        post = self.server()
        self.assertEqual(gateway_auth.check_session(URL), gateway_auth.SESSION_EXPIRED)
        post.assert_not_called()

    def test_no_verdict_is_unchecked(self):
        cases = {
            'connect timeout': requests.ConnectTimeout(),
            'read timeout': requests.ReadTimeout(),
            '500': FakeResponse(500, {}),
            '503': FakeResponse(503, {}),
            '429 rate limit': FakeResponse(429, {}),
            '200 without a token': FakeResponse(200, {}),
            '200 that is not JSON': FakeResponse(200, None),
        }
        for name, answer in cases.items():
            with self.subTest(name):
                gateway_auth._refresh_failed.clear()
                self.store(expires_in=-600)
                raises = isinstance(answer, BaseException)
                with mock.patch.object(gateway_auth.requests, 'post',
                                       side_effect=answer if raises else None,
                                       return_value=None if raises else answer):
                    self.assertEqual(gateway_auth.check_session(URL),
                                     gateway_auth.SESSION_UNCHECKED)

    def test_connection_refused_twice_is_unchecked(self):
        """The fast-failure retry runs, then gives no verdict."""
        self.store(expires_in=-600)
        post = self.server(requests.ConnectionError(), requests.ConnectionError())
        self.assertEqual(gateway_auth.check_session(URL), gateway_auth.SESSION_UNCHECKED)
        self.assertEqual(post.call_count, 2)

    def test_memo_keeps_the_reason_for_a_second_check(self):
        """One refused round trip per process; later checks reuse its verdict."""
        self.store(expires_in=-600)
        post = self.server(FakeResponse(401, {}))
        self.assertEqual(gateway_auth.check_session(URL), gateway_auth.SESSION_EXPIRED)
        self.assertEqual(gateway_auth.check_session(URL), gateway_auth.SESSION_EXPIRED)
        self.assertEqual(post.call_count, 1)

    def test_pinned_token_is_unchecked_and_never_contacts_the_server(self):
        self.store(expires_in=-600)
        post = self.server()
        with mock.patch.dict(os.environ, {gateway_auth.PINNED_TOKEN_ENV: 'ci-token'}):
            self.assertEqual(gateway_auth.check_session(URL),
                             gateway_auth.SESSION_UNCHECKED)
        post.assert_not_called()

    def test_access_token_for_still_returns_none_after_a_rejection(self):
        """The memo became a dict; its callers must see the same behaviour."""
        self.store(expires_in=-600)
        self.server(FakeResponse(401, {}))
        self.assertIsNone(gateway_auth.access_token_for(URL))
        self.assertEqual(gateway_auth._refresh_failed[gateway_auth._failed_key(URL)],
                         gateway_auth.REFRESH_REJECTED)


class WhoamiCommandTests(SessionCheckTestCase):
    """The real command, store and refresh path, with a fake server."""

    def whoami(self):
        return CliRunner().invoke(login_mod.whoami, [])

    def test_refused_session_reads_as_expired(self):
        self.store(expires_in=-600)
        self.server(FakeResponse(401, {}))
        result = self.whoami()
        self.assertEqual(result.exit_code, 0)
        self.assertIn(f'expired. Run: lager login {URL}', result.output)
        self.assertNotIn('active', result.output)

    def test_renewed_session_reads_as_active(self):
        self.store(expires_in=-600)
        self.server(FakeResponse(200, {'accessToken': make_token(900)}))
        result = self.whoami()
        self.assertIn('Session: active', result.output)

    def test_unreachable_server_reads_as_could_not_check(self):
        self.store(expires_in=-600)
        self.server(requests.ConnectTimeout())
        result = self.whoami()
        self.assertEqual(result.exit_code, 0)
        self.assertIn('could not check', result.output)
        self.assertNotIn('Session: active', result.output)
        self.assertNotIn('Session: expired', result.output)


if __name__ == '__main__':
    unittest.main()
