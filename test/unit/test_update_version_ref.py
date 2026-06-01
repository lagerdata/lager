# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for resolve_version_ref() in cli/commands/utility/update.py.

A `--version` pin must resolve a semver (with or without a leading 'v') to the
release TAG vX.Y.Z, while named branches (main, staging, feature branches) keep
using origin/<name>. Version branches are deprecated in favour of tags.
"""

import unittest

from cli.commands.utility.update import resolve_version_ref


class ResolveVersionRef(unittest.TestCase):
    def test_bare_semver_resolves_to_tag(self):
        # Bare 'X.Y.Z' (the old version-branch form) must pin to the tag.
        self.assertEqual(resolve_version_ref("0.18.5"), ("v0.18.5", "v0.18.5"))

    def test_v_prefixed_semver_resolves_to_tag(self):
        self.assertEqual(resolve_version_ref("v0.21.3"), ("v0.21.3", "v0.21.3"))

    def test_semver_prereleases_resolve_to_tags(self):
        # Common pre-release suffixes still pin to the tag.
        self.assertEqual(resolve_version_ref("0.18.5-rc1"), ("v0.18.5-rc1", "v0.18.5-rc1"))
        self.assertEqual(resolve_version_ref("v0.21.3-beta2"), ("v0.21.3-beta2", "v0.21.3-beta2"))
        self.assertEqual(resolve_version_ref("v0.22.0-alpha"), ("v0.22.0-alpha", "v0.22.0-alpha"))

    def test_custom_suffix_stays_a_branch(self):
        # A non-prerelease suffix (e.g. release-notes branch) is NOT a tag.
        self.assertEqual(
            resolve_version_ref("v0.21.3-notes"),
            ("v0.21.3-notes", "origin/v0.21.3-notes"),
        )

    def test_named_branch_uses_origin(self):
        self.assertEqual(resolve_version_ref("main"), ("main", "origin/main"))
        self.assertEqual(resolve_version_ref("staging"), ("staging", "origin/staging"))

    def test_feature_branch_uses_origin(self):
        self.assertEqual(
            resolve_version_ref("de/lager-net"),
            ("de/lager-net", "origin/de/lager-net"),
        )

    def test_non_semver_versionish_names_are_branches(self):
        # Not a full X.Y.Z semver -> treated as a branch, not a tag.
        self.assertEqual(resolve_version_ref("0.18"), ("0.18", "origin/0.18"))
        self.assertEqual(
            resolve_version_ref("v0.21.3-notes"),
            ("v0.21.3-notes", "origin/v0.21.3-notes"),
        )


if __name__ == "__main__":
    unittest.main()
