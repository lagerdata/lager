# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

import pytest
from cli.box_storage import format_lock_user


class TestFormatLockUser:
    def test_stout_format_returns_email(self):
        assert format_lock_user('stout:bb61a442-4840-4df0-9a69-de01e57e627b:user@example.com') == 'user@example.com'

    def test_regular_user_unchanged(self):
        assert format_lock_user('localuser') == 'localuser'

    def test_stout_malformed_one_colon(self):
        assert format_lock_user('stout:malformed') == 'stout:malformed'

    def test_stout_prefix_only(self):
        assert format_lock_user('stout:') == 'stout:'

    def test_none_returns_none(self):
        assert format_lock_user(None) is None

    def test_empty_string_returns_empty(self):
        assert format_lock_user('') == ''

    def test_email_with_colons_preserved(self):
        assert format_lock_user('stout:uuid:user:with:colons@example.com') == 'user:with:colons@example.com'
