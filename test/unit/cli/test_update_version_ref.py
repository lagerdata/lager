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

A full 40-character commit SHA resolves to itself in all three positions. That is
what lets CI pin a bench run to the commit it checked out instead of to a moving
branch (#326): `main` is re-resolved against origin/main on every probe, so a
merge landing mid-run makes a box read as stale while it runs exactly the commit
under test. A SHA needs no tag-style refspec -- a raw object id resolves once the
object is local, which is precisely what a tag NAME cannot do.
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


SHA = "5d84c68612384eed2854638c1e0941a4ff8b7893"


class ResolveCommitSha(unittest.TestCase):
    """A full commit SHA is its own checkout, reset and fetch ref."""

    def test_full_sha_resolves_to_itself(self):
        self.assertEqual(resolve_version_ref(SHA), (SHA, SHA, SHA))

    def test_sha_is_lowercased(self):
        # git accepts either case; github.sha is lowercase. Normalising means a
        # hand-typed uppercase SHA compares equal to what CI would have sent.
        self.assertEqual(resolve_version_ref(SHA.upper()), (SHA, SHA, SHA))

    def test_a_sha_never_resolves_through_origin(self):
        # The whole point: a SHA must NOT become origin/<sha>, which is what the
        # branch arm would have made of it and which resolves to nothing.
        _, reset, _ = resolve_version_ref(SHA)
        self.assertFalse(reset.startswith("origin/"))

    def test_short_prefixes_stay_branches(self):
        # Only the full 40 is accepted: a short hex prefix cannot be told apart
        # from a branch name, and every caller that needs this has the full SHA.
        for short in (SHA[:7], SHA[:8], SHA[:12], SHA[:39]):
            with self.subTest(short=short):
                self.assertEqual(
                    resolve_version_ref(short),
                    (short, f"origin/{short}", short),
                )

    def test_forty_one_characters_stays_a_branch(self):
        over = SHA + "a"
        self.assertEqual(resolve_version_ref(over), (over, f"origin/{over}", over))

    def test_forty_non_hex_characters_stays_a_branch(self):
        # Same length, one character outside [0-9a-f] -- a plausible branch name.
        not_hex = "z" + SHA[1:]
        self.assertEqual(
            resolve_version_ref(not_hex), (not_hex, f"origin/{not_hex}", not_hex)
        )

    def test_a_sha_does_not_collide_with_the_tag_arm(self):
        # A SHA is all-hex and could in principle contain a digit run; make sure
        # the semver arm is still the one that wins for real versions.
        checkout, _, fetch = resolve_version_ref("v0.40.0")
        self.assertEqual(checkout, "v0.40.0")
        self.assertTrue(fetch.startswith("refs/tags/"))
