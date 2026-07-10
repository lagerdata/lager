# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import pytest
from cli.box_storage import format_lock_user


class TestFormatLockUser:
    def test_reservation_format_returns_email(self):
        # <origin>:<id>:<email> reservations (written by the web dashboard)
        assert format_lock_user('dashboard:bb61a442-4840-4df0-9a69-de01e57e627b:user@example.com') == 'user@example.com'

    def test_regular_user_unchanged(self):
        assert format_lock_user('localuser') == 'localuser'

    def test_reservation_malformed_one_colon(self):
        assert format_lock_user('dashboard:malformed') == 'dashboard:malformed'

    def test_reservation_prefix_only(self):
        assert format_lock_user('dashboard:') == 'dashboard:'

    def test_none_returns_none(self):
        assert format_lock_user(None) is None

    def test_empty_string_returns_empty(self):
        assert format_lock_user('') == ''

    def test_email_with_colons_preserved(self):
        assert format_lock_user('dashboard:uuid:user:with:colons@example.com') == 'user:with:colons@example.com'

    def test_non_email_reservation_left_visible(self):
        # Without an @ in the last segment we can't be sure it's a
        # reservation, so the raw string stays visible.
        assert format_lock_user('tcp:host:5000') == 'tcp:host:5000'
