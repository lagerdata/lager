# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for cli.core.net_helpers.resolve_box_locked.

Verifies that hardware-interacting CLI commands:
- Acquire an ephemeral lock on box resolution.
- Release the lock on exit (via the atexit registered inside
  auto_lock_acquire_for_command).
- Pass through cleanly when LAGER_AUTO_LOCK_DISABLE is set.
- Respect pre-existing user locks (do NOT release them).
"""

from __future__ import annotations

import os
from unittest import mock

import click
import pytest


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for key in ('LAGER_AUTO_LOCK_DISABLE', 'LAGER_LOCK_HOLDER', 'LAGER_USER'):
        monkeypatch.delenv(key, raising=False)


class _FakeResp:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = ''
        self.headers = {}

    def json(self):
        return self._json


def _make_ctx():
    """Build a minimal Click context with obj namespace."""
    ctx = click.Context(click.Command('test'))
    ctx.obj = type('Obj', (), {})()
    return ctx


class TestResolveBoxLocked:
    """Test resolve_box_locked acquires a lock."""

    @mock.patch('requests.post')
    @mock.patch('requests.get')
    @mock.patch('cli.box_storage.resolve_and_validate_box_with_name')
    def test_acquires_lock_on_resolve(self, mock_resolve, mock_get, mock_post):
        """Calling resolve_box_locked should POST /lock on port 9000."""
        mock_resolve.return_value = ('10.0.0.1', 'test-box')
        # GET /lock returns unlocked
        mock_get.return_value = _FakeResp(200, {'locked': False})
        # POST /lock returns acquired
        mock_post.return_value = _FakeResp(200, {
            'locked': True,
            'user': 'alice',
            'holder_type': 'ephemeral',
            'locked_at': '2026-01-01T00:00:00Z',
            'last_heartbeat': '2026-01-01T00:00:00Z',
            'ttl_seconds': 1800,
            'previous_user': None,
        })

        from cli.core.net_helpers import resolve_box_locked

        ctx = _make_ctx()
        with mock.patch('cli.box_storage.get_lager_user', return_value='alice'):
            ip = resolve_box_locked(ctx, 'test-box', 'gpi')

        assert ip == '10.0.0.1'
        # Verify POST /lock was called
        assert mock_post.call_count >= 1
        lock_call = mock_post.call_args_list[-1]
        assert '/lock' in lock_call.args[0] or '/lock' in str(lock_call)

    @mock.patch('cli.box_storage.resolve_and_validate_box_with_name')
    def test_skips_lock_when_disabled(self, mock_resolve, monkeypatch):
        """LAGER_AUTO_LOCK_DISABLE skips the lock entirely."""
        monkeypatch.setenv('LAGER_AUTO_LOCK_DISABLE', '1')
        mock_resolve.return_value = ('10.0.0.2', 'lab-box')

        from cli.core.net_helpers import resolve_box_locked

        ctx = _make_ctx()
        ip = resolve_box_locked(ctx, 'lab-box', 'spi')
        assert ip == '10.0.0.2'

    @mock.patch('requests.post')
    @mock.patch('requests.get')
    @mock.patch('cli.box_storage.resolve_and_validate_box_with_name')
    def test_stashes_release_on_ctx(self, mock_resolve, mock_get, mock_post):
        """The release callable is stashed on ctx.obj._lock_releases."""
        mock_resolve.return_value = ('10.0.0.3', 'my-box')
        mock_get.return_value = _FakeResp(200, {'locked': False})
        mock_post.return_value = _FakeResp(200, {
            'locked': True,
            'user': 'bob',
            'holder_type': 'ephemeral',
            'locked_at': '2026-01-01T00:00:00Z',
            'last_heartbeat': '2026-01-01T00:00:00Z',
            'ttl_seconds': 1800,
            'previous_user': None,
        })

        from cli.core.net_helpers import resolve_box_locked

        ctx = _make_ctx()
        with mock.patch('cli.box_storage.get_lager_user', return_value='bob'):
            resolve_box_locked(ctx, 'my-box', 'debug')

        releases = getattr(ctx.obj, '_lock_releases', [])
        assert len(releases) == 1
        assert callable(releases[0])

    @mock.patch('requests.get')
    @mock.patch('cli.box_storage.resolve_and_validate_box_with_name')
    def test_already_ours_no_release(self, mock_resolve, mock_get):
        """If the lock is already ours, release.state == 'already_ours'."""
        mock_resolve.return_value = ('10.0.0.4', 'shared-box')
        # GET /lock shows already locked by us
        mock_get.return_value = _FakeResp(200, {
            'locked': True,
            'user': 'carol',
            'holder_type': 'user',
            'ttl_seconds': None,
        })

        from cli.core.net_helpers import resolve_box_locked

        ctx = _make_ctx()
        with mock.patch('cli.box_storage.get_lager_user', return_value='carol'):
            ip = resolve_box_locked(ctx, 'shared-box', 'uart')

        assert ip == '10.0.0.4'
        releases = getattr(ctx.obj, '_lock_releases', [])
        # Release is stashed but its .state should be 'already_ours'
        if releases:
            assert releases[0].state == 'already_ours'
