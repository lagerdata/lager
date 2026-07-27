#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``cli/commands/utility/login.py`` -- the `lager login`,
`lager logout` and `lager whoami` command layer.

This is the auth entry point. ``cli/gateway_auth.py`` (the transport and token
store beneath it) is well covered by ``cli/tests/test_gateway_auth.py``; the
click layer on top had nothing. It is thin but not trivial: it owns the
display-name fallback, the MFA prompt callback, the rstrip that decides which
stored session `logout` erases, and the four states `whoami` renders.

The gateway_auth functions are mocked throughout -- these tests are about the
command layer's own decisions, not the protocol, and nothing here should touch
``~/.lager_gateway_auth``. ``LAGER_GATEWAY_AUTH_FILE`` is redirected anyway as a
belt-and-braces guard so a mocking mistake cannot write to a real session file.
"""

import os
import tempfile
import unittest
from importlib import import_module
from unittest import mock

from click.testing import CliRunner

from cli.errors import LagerError

# import_module, NOT `from cli.commands.utility import login`. That package's
# __init__ does `from .login import login, logout, whoami`, so the name `login`
# in the package namespace is the click COMMAND, not this module -- and
# patching `gateway_auth` on a Command object silently patches nothing.
login_mod = import_module('cli.commands.utility.login')

URL = 'https://auth.example.com'


class LoginCommandTestCase(unittest.TestCase):

    def setUp(self):
        self.runner = CliRunner()
        self.tmp = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {
            'LAGER_GATEWAY_AUTH_FILE': os.path.join(self.tmp, 'auth.json'),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        import shutil
        self.addCleanup(shutil.rmtree, self.tmp, True)


class LoginTests(LoginCommandTestCase):

    def invoke(self, args, **kwargs):
        return self.runner.invoke(login_mod.login, args, **kwargs)

    def test_successful_login_reports_display_name(self):
        with mock.patch.object(login_mod.gateway_auth, 'login',
                               return_value={'displayName': 'Ada L'}) as gw:
            result = self.invoke([URL, '--email', 'a@b.c', '--password', 'pw'])
        self.assertEqual(result.exit_code, 0)
        self.assertIn('Ada L', result.output)
        gw.assert_called_once()
        self.assertEqual(gw.call_args.args[:3], (URL, 'a@b.c', 'pw'))

    def test_falls_back_to_email_from_the_response(self):
        with mock.patch.object(login_mod.gateway_auth, 'login',
                               return_value={'email': 'server@b.c'}):
            result = self.invoke([URL, '--email', 'typed@b.c', '--password', 'pw'])
        self.assertIn('server@b.c', result.output)

    def test_falls_back_to_the_typed_email_when_user_object_is_empty(self):
        with mock.patch.object(login_mod.gateway_auth, 'login', return_value={}):
            result = self.invoke([URL, '--email', 'typed@b.c', '--password', 'pw'])
        self.assertIn('typed@b.c', result.output)

    def test_empty_display_name_falls_through_to_email(self):
        """`or` chaining means '' must not win over a usable email."""
        with mock.patch.object(login_mod.gateway_auth, 'login',
                               return_value={'displayName': '', 'email': 'e@b.c'}):
            result = self.invoke([URL, '--email', 'typed@b.c', '--password', 'pw'])
        self.assertIn('e@b.c', result.output)

    def test_trailing_slash_is_stripped_from_the_reported_url(self):
        with mock.patch.object(login_mod.gateway_auth, 'login', return_value={}):
            result = self.invoke([URL + '/', '--email', 'a@b.c', '--password', 'pw'])
        self.assertIn(URL + ' ', result.output + ' ')
        self.assertNotIn(URL + '/ as', result.output)

    def test_password_is_not_echoed(self):
        with mock.patch.object(login_mod.gateway_auth, 'login', return_value={}):
            result = self.invoke([URL, '--email', 'a@b.c', '--password', 'hunter2'])
        self.assertNotIn('hunter2', result.output)

    def test_credentials_are_prompted_when_omitted(self):
        with mock.patch.object(login_mod.gateway_auth, 'login',
                               return_value={}) as gw:
            result = self.runner.invoke(login_mod.login, [URL],
                                        input='a@b.c\nhunter2\n')
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(gw.call_args.args[1:3], ('a@b.c', 'hunter2'))
        self.assertNotIn('hunter2', result.output)

    def test_mfa_prompt_is_wired_and_collects_a_code(self):
        """The command passes a callable; gateway_auth invokes it on demand."""
        captured = {}

        def fake_login(url, email, password, mfa_code_prompt=None):
            captured['code'] = mfa_code_prompt()
            return {'email': email}

        with mock.patch.object(login_mod.gateway_auth, 'login', side_effect=fake_login):
            result = self.runner.invoke(
                login_mod.login, [URL, '--email', 'a@b.c', '--password', 'pw'],
                input='123456\n')
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(captured['code'], '123456')

    def test_auth_failure_propagates_as_a_lager_error(self):
        with mock.patch.object(login_mod.gateway_auth, 'login',
                               side_effect=LagerError('Login failed: bad password')):
            result = self.invoke([URL, '--email', 'a@b.c', '--password', 'wrong'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('Login failed', result.output)

    def test_url_argument_is_required(self):
        result = self.invoke([])
        self.assertNotEqual(result.exit_code, 0)


class LogoutTests(LoginCommandTestCase):

    def test_logout_of_one_server_strips_the_trailing_slash(self):
        """The store is keyed on the rstripped URL, so this must match."""
        with mock.patch.object(login_mod.gateway_auth, 'clear_login') as clear:
            result = self.runner.invoke(login_mod.logout, [URL + '/'])
        self.assertEqual(result.exit_code, 0)
        clear.assert_called_once_with(URL)

    def test_logout_without_a_url_clears_everything(self):
        with mock.patch.object(login_mod.gateway_auth, 'clear_login') as clear:
            result = self.runner.invoke(login_mod.logout, [])
        self.assertEqual(result.exit_code, 0)
        clear.assert_called_once_with(None)
        self.assertIn('all auth servers', result.output)

    def test_logout_names_the_server_it_cleared(self):
        with mock.patch.object(login_mod.gateway_auth, 'clear_login'):
            result = self.runner.invoke(login_mod.logout, [URL])
        self.assertIn(URL, result.output)


class WhoamiTests(LoginCommandTestCase):

    def invoke(self, status):
        with mock.patch.object(login_mod.gateway_auth, 'auth_status', return_value=status):
            return self.runner.invoke(login_mod.whoami, [])

    def _entry(self, **over):
        base = {'url': URL, 'email': 'a@b.c', 'expires_in': 300,
                'refreshable': True, 'boxes': []}
        base.update(over)
        return base

    def test_signed_out_explains_that_plain_boxes_need_no_login(self):
        result = self.invoke([])
        self.assertEqual(result.exit_code, 0)
        self.assertIn('Not signed in', result.output)
        self.assertIn('lager login', result.output)
        self.assertIn(login_mod.ACCESS_DOCS_URL, result.output)

    def test_active_session(self):
        result = self.invoke([self._entry()])
        self.assertIn('a@b.c', result.output)
        self.assertIn('active', result.output)

    def test_unknown_user_placeholder(self):
        result = self.invoke([self._entry(email=None)])
        self.assertIn('unknown user', result.output)

    def test_lapsed_but_refreshable_reads_as_active(self):
        """A stored refresh cookie means the user need do nothing.

        The command deliberately does not surface the short-lived access
        token's expiry, which lapses every few minutes by design.
        """
        result = self.invoke([self._entry(expires_in=-10, refreshable=True)])
        self.assertIn('renews automatically', result.output)
        self.assertNotIn('expired', result.output)

    def test_expired_without_refresh_tells_the_user_what_to_run(self):
        result = self.invoke([self._entry(expires_in=-10, refreshable=False)])
        self.assertIn('expired', result.output)
        self.assertIn(f'lager login {URL}', result.output)

    def test_expires_in_none_falls_back_to_the_refreshable_check(self):
        """`expires_in` is None when no token is stored at all."""
        result = self.invoke([self._entry(expires_in=None, refreshable=False)])
        self.assertIn('expired', result.output)

    def test_zero_expiry_counts_as_active(self):
        """Boundary: the check is `>= 0`, not `> 0`."""
        result = self.invoke([self._entry(expires_in=0)])
        self.assertIn('active', result.output)
        self.assertNotIn('expired', result.output)

    def test_multiple_servers_are_all_listed(self):
        other = 'https://auth2.example.com'
        result = self.invoke([self._entry(), self._entry(url=other, email='b@c.d')])
        self.assertIn(URL, result.output)
        self.assertIn(other, result.output)
        self.assertIn('b@c.d', result.output)

    def test_whoami_never_prints_a_token(self):
        """auth_status returns no token material; guard against that changing."""
        result = self.invoke([self._entry()])
        for leak in ('accessToken', 'Bearer', 'eyJ'):
            self.assertNotIn(leak, result.output)


if __name__ == '__main__':
    unittest.main()
