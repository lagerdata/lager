# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for resolve_version_ref() in cli/commands/utility/update.py.

A `--version` pin must resolve a semver (with or without a leading 'v') to the
release TAG vX.Y.Z, while named branches (main, staging, feature branches) keep
using origin/<name>. Version branches are deprecated in favour of tags.

resolve_version_ref() returns (checkout, reset, fetch). For tags the fetch arg is
an explicit refspec so the tag becomes a local ref (`git fetch origin <tag>` only
sets FETCH_HEAD, which breaks `git rev-list`/`git checkout <tag>`).
"""

import unittest

from cli.commands.utility.update import resolve_version_ref


class ResolveVersionRef(unittest.TestCase):
    def test_bare_semver_resolves_to_tag(self):
        # Bare 'X.Y.Z' (the old version-branch form) must pin to the tag.
        self.assertEqual(
            resolve_version_ref("0.18.5"),
            ("v0.18.5", "v0.18.5", "refs/tags/v0.18.5:refs/tags/v0.18.5"),
        )

    def test_v_prefixed_semver_resolves_to_tag(self):
        self.assertEqual(
            resolve_version_ref("v0.21.3"),
            ("v0.21.3", "v0.21.3", "refs/tags/v0.21.3:refs/tags/v0.21.3"),
        )

    def test_semver_prereleases_resolve_to_tags(self):
        # Common pre-release suffixes still pin to the tag.
        self.assertEqual(
            resolve_version_ref("0.18.5-rc1"),
            ("v0.18.5-rc1", "v0.18.5-rc1", "refs/tags/v0.18.5-rc1:refs/tags/v0.18.5-rc1"),
        )
        self.assertEqual(
            resolve_version_ref("v0.21.3-beta2"),
            ("v0.21.3-beta2", "v0.21.3-beta2", "refs/tags/v0.21.3-beta2:refs/tags/v0.21.3-beta2"),
        )

    def test_named_branch_uses_origin(self):
        # Branches: checkout the name, reset to origin/<name>, fetch the name.
        self.assertEqual(resolve_version_ref("main"), ("main", "origin/main", "main"))
        self.assertEqual(resolve_version_ref("staging"), ("staging", "origin/staging", "staging"))

    def test_feature_branch_uses_origin(self):
        self.assertEqual(
            resolve_version_ref("de/lager-net"),
            ("de/lager-net", "origin/de/lager-net", "de/lager-net"),
        )

    def test_custom_or_partial_versions_stay_branches(self):
        # A non-prerelease suffix (release-notes branch) and a partial version
        # are NOT tags -> treated as branches.
        self.assertEqual(
            resolve_version_ref("v0.21.3-notes"),
            ("v0.21.3-notes", "origin/v0.21.3-notes", "v0.21.3-notes"),
        )
        self.assertEqual(resolve_version_ref("0.18"), ("0.18", "origin/0.18", "0.18"))


if __name__ == "__main__":
    unittest.main()
