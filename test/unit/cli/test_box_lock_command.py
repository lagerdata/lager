#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``cli/commands/box/lock.py`` -- the `lager boxes lock` /
`lager boxes unlock` command layer.

The helpers underneath (``box_storage.format_lock_user`` and friends) are well
covered by ``test/unit/cli/test_box_lock_helpers.py`` and
``test/test_format_lock_user.py``; the command wrapper itself had nothing. What
lives only here:

  * the request BODY -- `holder_type: "user"` and `ttl_seconds: None` are what
    keep an explicit reservation immune to the heartbeat-driven auto-reap that
    ephemeral `lager python` locks participate in. A regression that dropped
    either field would silently make `lager boxes lock` expire on its own,
    which no helper test would notice.
  * exit codes. A lock that fails to acquire MUST exit non-zero or a CI job
    proceeds onto a box someone else holds.
  * the root warning, and `--user` overriding it.
  * `--force` reaching the wire on unlock.

``resolve_and_validate_box_with_name`` and ``requests.post`` are patched on the
lock module, so nothing resolves a real box or opens a socket.
"""

import unittest
from importlib import import_module
from unittest import mock

from click.testing import CliRunner

# import_module for the same reason as test_login_commands.py: the package
# __init__ re-exports these as click Commands, shadowing the module name.
lock_mod = import_module('cli.commands.box.lock')


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class LockCommandTestCase(unittest.TestCase):

    def setUp(self):
        self.runner = CliRunner()
        self.resolve = mock.patch.object(
            lock_mod, 'resolve_and_validate_box_with_name',
            return_value=('10.0.0.5', 'bench-1')).start()
        self.addCleanup(mock.patch.stopall)
        # _check_gateway is imported inside the function from box_storage; it
        # passes a non-gated response straight through.
        mock.patch('cli.box_storage._check_gateway', side_effect=lambda r, ip: r).start()
        mock.patch('cli.gateway_auth.auth_headers_for_box',
                   return_value={'Authorization': 'Bearer tok'}).start()

    def post(self, response):
        return mock.patch.object(lock_mod.requests, 'post', return_value=response)


class LockTests(LockCommandTestCase):

    def test_success_reports_the_holder_and_exits_zero(self):
        with self.post(FakeResponse(200, {'user': 'ada'})):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                result = self.runner.invoke(lock_mod.lock, ['--box', 'bench-1'])
        self.assertEqual(result.exit_code, 0)
        self.assertIn('bench-1', result.output)
        self.assertIn('ada', result.output)

    def test_request_body_pins_the_no_expiry_reservation(self):
        """holder_type=user + ttl_seconds=None keep the lock off the auto-reap."""
        with self.post(FakeResponse(200, {'user': 'ada'})) as post:
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                self.runner.invoke(lock_mod.lock, ['--box', 'bench-1'])
        body = post.call_args.kwargs['json']
        self.assertEqual(body['holder_type'], 'user')
        self.assertIsNone(body['ttl_seconds'])
        self.assertEqual(body['user'], 'ada')

    def test_posts_to_the_resolved_ip_on_port_9000(self):
        with self.post(FakeResponse(200, {'user': 'ada'})) as post:
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                self.runner.invoke(lock_mod.lock, ['--box', 'bench-1'])
        self.assertEqual(post.call_args.args[0], 'http://10.0.0.5:9000/lock')

    def test_auth_headers_are_attached(self):
        with self.post(FakeResponse(200, {'user': 'ada'})) as post:
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                self.runner.invoke(lock_mod.lock, ['--box', 'bench-1'])
        self.assertEqual(post.call_args.kwargs['headers'],
                         {'Authorization': 'Bearer tok'})

    def test_lock_skips_the_lock_check_when_resolving(self):
        """Resolving must not itself trip the "box is locked" guard."""
        with self.post(FakeResponse(200, {'user': 'ada'})):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                self.runner.invoke(lock_mod.lock, ['--box', 'bench-1'])
        self.assertTrue(self.resolve.call_args.kwargs['_skip_lock_check'])

    def test_conflict_exits_nonzero_and_names_the_holder(self):
        """The important one: a CI job must not continue past a taken box."""
        body = {'lock': {'user': 'bob', 'locked_at': '2026-07-27T10:00:00Z'}}
        with self.post(FakeResponse(409, body)):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                result = self.runner.invoke(lock_mod.lock, ['--box', 'bench-1'])
        self.assertEqual(result.exit_code, 1)
        self.assertIn('already locked', result.output)
        self.assertIn('bob', result.output)
        self.assertIn('2026-07-27T10:00:00Z', result.output)

    def test_unexpected_status_exits_nonzero(self):
        with self.post(FakeResponse(500)):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                result = self.runner.invoke(lock_mod.lock, ['--box', 'bench-1'])
        self.assertEqual(result.exit_code, 1)
        self.assertIn('500', result.output)

    def test_unreachable_box_exits_nonzero(self):
        with mock.patch.object(lock_mod.requests, 'post',
                               side_effect=lock_mod.requests.exceptions.ConnectionError('refused')):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                result = self.runner.invoke(lock_mod.lock, ['--box', 'bench-1'])
        self.assertEqual(result.exit_code, 1)
        self.assertIn('Could not reach', result.output)

    def test_user_option_overrides_the_detected_user(self):
        with self.post(FakeResponse(200, {'user': 'ada'})) as post:
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='root') as g:
                self.runner.invoke(lock_mod.lock,
                                   ['--box', 'bench-1', '--user', 'ada'])
        self.assertEqual(post.call_args.kwargs['json']['user'], 'ada')
        g.assert_not_called()

    def test_root_triggers_the_docker_warning(self):
        with self.post(FakeResponse(200, {'user': 'root'})):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='root'):
                result = self.runner.invoke(lock_mod.lock, ['--box', 'bench-1'])
        self.assertIn('locking as root', result.output)
        self.assertIn('--user', result.output)

    def test_no_root_warning_for_a_normal_user(self):
        with self.post(FakeResponse(200, {'user': 'ada'})):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                result = self.runner.invoke(lock_mod.lock, ['--box', 'bench-1'])
        self.assertNotIn('locking as root', result.output)

    def test_explicit_root_user_still_warns(self):
        """--user root is still root; the warning is about the value, not its source."""
        with self.post(FakeResponse(200, {'user': 'root'})):
            result = self.runner.invoke(lock_mod.lock,
                                        ['--box', 'bench-1', '--user', 'root'])
        self.assertIn('locking as root', result.output)

    def test_box_option_is_required(self):
        result = self.runner.invoke(lock_mod.lock, [])
        self.assertNotEqual(result.exit_code, 0)


class UnlockTests(LockCommandTestCase):

    def test_success_exits_zero(self):
        with self.post(FakeResponse(200)):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                result = self.runner.invoke(lock_mod.unlock, ['--box', 'bench-1'])
        self.assertEqual(result.exit_code, 0)
        self.assertIn('now unlocked', result.output)

    def test_posts_to_the_unlock_endpoint(self):
        with self.post(FakeResponse(200)) as post:
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                self.runner.invoke(lock_mod.unlock, ['--box', 'bench-1'])
        self.assertEqual(post.call_args.args[0], 'http://10.0.0.5:9000/unlock')

    def test_force_defaults_to_false_and_is_always_sent(self):
        with self.post(FakeResponse(200)) as post:
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                self.runner.invoke(lock_mod.unlock, ['--box', 'bench-1'])
        self.assertIs(post.call_args.kwargs['json']['force'], False)

    def test_force_flag_reaches_the_wire(self):
        with self.post(FakeResponse(200)) as post:
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                self.runner.invoke(lock_mod.unlock, ['--box', 'bench-1', '--force'])
        self.assertIs(post.call_args.kwargs['json']['force'], True)

    def test_forbidden_exits_nonzero_and_suggests_force(self):
        body = {'lock': {'user': 'bob'}}
        with self.post(FakeResponse(403, body)):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                result = self.runner.invoke(lock_mod.unlock, ['--box', 'bench-1'])
        self.assertEqual(result.exit_code, 1)
        self.assertIn('bob', result.output)
        self.assertIn('--force', result.output)

    def test_unexpected_status_exits_nonzero(self):
        with self.post(FakeResponse(500)):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                result = self.runner.invoke(lock_mod.unlock, ['--box', 'bench-1'])
        self.assertEqual(result.exit_code, 1)

    def test_unreachable_box_exits_nonzero(self):
        with mock.patch.object(lock_mod.requests, 'post',
                               side_effect=lock_mod.requests.exceptions.Timeout('slow')):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                result = self.runner.invoke(lock_mod.unlock, ['--box', 'bench-1'])
        self.assertEqual(result.exit_code, 1)
        self.assertIn('Could not reach', result.output)

    def test_unlock_has_no_user_override(self):
        """Asymmetry with `lock`, pinned deliberately.

        `lock --user` exists for the Docker-root case; `unlock` has no such
        option and always uses the detected user, relying on --force instead.
        """
        result = self.runner.invoke(lock_mod.unlock,
                                    ['--box', 'bench-1', '--user', 'ada'])
        self.assertNotEqual(result.exit_code, 0)


class BothCommandsTests(LockCommandTestCase):

    def test_display_name_falls_back_to_the_typed_box_when_unnamed(self):
        """resolve() returns (ip, None) for an IP-addressed box."""
        self.resolve.return_value = ('10.0.0.5', None)
        with self.post(FakeResponse(200, {'user': 'ada'})):
            with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                result = self.runner.invoke(lock_mod.lock, ['--box', '10.0.0.5'])
        self.assertIn('10.0.0.5', result.output)

    def test_both_use_a_bounded_timeout(self):
        """No timeout would hang a CI job forever against a wedged box."""
        for cmd in (lock_mod.lock, lock_mod.unlock):
            with self.subTest(cmd=cmd.name):
                with self.post(FakeResponse(200, {'user': 'ada'})) as post:
                    with mock.patch.object(lock_mod, 'get_lager_user', return_value='ada'):
                        self.runner.invoke(cmd, ['--box', 'bench-1'])
                self.assertEqual(post.call_args.kwargs['timeout'], 5)


if __name__ == '__main__':
    unittest.main()
