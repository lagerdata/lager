#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the fetch step of `lager update` -- `_fetch_shell_script`.

The script runs over SSH against a box; nothing here spins up SSH. What is
pinned is the command line it builds, because one flag in it decides whether a
box can be updated at all.

A box whose clone predates a re-created tag holds that tag at a different
object than origin does. An unforced fetch of `refs/tags/<tag>:refs/tags/<tag>`
refuses it ("would clobber existing tag") and exits non-zero, which this flow
reads as a failed fetch -- so the box could not be updated, and no later
attempt could fix it either, because every attempt fetched the same way. Origin
is authoritative for a checkout this flow resets hard a few steps later, so the
fetch forces.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from cli.commands.utility.update import _fetch_shell_script, resolve_version_ref


class FetchShellScript(unittest.TestCase):
    def test_tag_fetch_is_forced(self):
        _checkout, git_ref, fetch_ref = resolve_version_ref('0.46.1')
        script = _fetch_shell_script(fetch_ref, git_ref)
        self.assertIn(
            'git fetch origin --force refs/tags/v0.46.1:refs/tags/v0.46.1',
            script,
        )

    def test_branch_fetch_is_forced_too(self):
        # Harmless for a branch -- git already forces refs/remotes/origin/*
        # through the default refspec -- and keeping one code path means the
        # tag case cannot be the one that gets forgotten.
        _checkout, git_ref, fetch_ref = resolve_version_ref('main')
        self.assertIn('git fetch origin --force main', _fetch_shell_script(fetch_ref, git_ref))

    def test_rc_marker_still_precedes_the_divergence_count(self):
        # The caller splits on this marker to tell a fetch failure from a
        # rev-list result; forcing the fetch must not disturb the order.
        script = _fetch_shell_script('main', 'origin/main')
        self.assertLess(script.index('LAGER_FETCH_RC=$?'), script.index('git rev-list'))
        self.assertLess(script.index('git fetch'), script.index('LAGER_FETCH_RC=$?'))

    def test_fetch_output_is_captured_not_discarded(self):
        # 2>&1 keeps git's own message (the rejection text included) in the
        # output the caller classifies; losing it would turn a diagnosable
        # failure into a bare non-zero rc.
        self.assertIn('2>&1', _fetch_shell_script('main', 'origin/main'))


if __name__ == '__main__':
    unittest.main()
