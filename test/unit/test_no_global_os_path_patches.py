# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
No test may patch os.path — it is never the module under test's private
namespace.

`import os` gives every module the SAME os.path object (the stdlib posixpath /
ntpath), so `mock.patch.object(some_module.os.path, "exists", ...)` looks
scoped and is actually process-global: it rewrites the answer for every
os.path.exists call everywhere. Worse, the blast radius depends on the
interpreter: through Python 3.13, pathlib.Path.exists() went straight to
stat() and never saw the patch; on 3.14 it delegates to os.path.exists, so
the same patch silently neuters every Path.exists() in the process. That is
exactly how test_box_ssh_identity.py broke install.py's deploy-script check
on the 3.14 compat gate while staying green on 3.11 — the test's instrument
could not reach reality, so it measured the instrument.

The fix is always one of:
  * patch the module's own seam (e.g. _ssh.lager_box_key_if_present), or
  * hand the code a real path in a temp dir when the path is a parameter, or
  * redirect HOME to a temp dir when the path comes from expanduser.

This is the same enforcement style as test_sudoers_contract.py: scan the
tree, allow nothing, and make an exception a visible edit to this file.
"""

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
THIS_FILE = pathlib.Path(__file__).resolve()

#: Every directory whose tests any CI gate runs (see unit-tests.yml).
TEST_TREES = ("test", "cli/tests")

# mock.patch.object(anything.os.path, "exists"/"isfile"/...): the first
# argument names os.path itself, so whatever attribute is swapped, the swap
# is global. `\bos\.path\b` will not match e.g. `myos.path` (no word
# boundary) or a module that merely ends in `_os` (the dot is required to
# be a real attribute step or the start of the name).
_PATCH_OBJECT = re.compile(r"patch\.object\(\s*(?:[\w.]*\.)?os\.path\b")

# mock.patch("os.path.exists") / mock.patch("pkg.mod.os.path.exists"): the
# string form of the same thing — the target resolves to the shared module
# either way.
_PATCH_STRING = re.compile(r"patch\(\s*[\"'](?:[\w.]*\.)?os\.path\.\w+[\"']")


def _test_files():
    for tree in TEST_TREES:
        root = REPO_ROOT / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.resolve() != THIS_FILE:
                yield path


def _offenders():
    """(path:line, matched text) for every os.path patch in the test trees.

    A line is considered from its first `#` onward to be commentary, so prose
    ABOUT the anti-pattern (like the comments the fixes left behind) does not
    trip the scan. The patterns themselves are single-token-contiguous, so a
    call split across lines is still caught by scanning whole-file text —
    only the report's line number comes from the match position.
    """
    found = []
    for path in _test_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        code = "\n".join(
            line if line.find("#") < 0 else line[:line.find("#")]
            for line in text.splitlines()
        )
        for pattern in (_PATCH_OBJECT, _PATCH_STRING):
            for m in pattern.finditer(code):
                lineno = code.count("\n", 0, m.start()) + 1
                rel = path.relative_to(REPO_ROOT)
                found.append((f"{rel}:{lineno}", m.group(0)))
    return found


class NoGlobalOsPathPatches(unittest.TestCase):
    def test_no_test_patches_os_path(self):
        self.assertEqual(
            _offenders(), [],
            "\n\nThese tests patch os.path, which is process-global (and on "
            "Python >= 3.14 also rewrites every pathlib.Path.exists()). "
            "Patch the module's own seam, or use a real temp path — see this "
            "file's docstring.",
        )


class TheGuardItselfWorks(unittest.TestCase):
    """A scanner that silently matches nothing is worse than no scanner —
    these pin each pattern against the exact shapes that shipped."""

    def test_catches_patch_object_on_a_module_alias(self):
        self.assertTrue(_PATCH_OBJECT.search(
            'mock.patch.object(_ssh.os.path, "exists", return_value=True)'))
        self.assertTrue(_PATCH_OBJECT.search(
            'patch.object(u.os.path, "isfile", return_value=False)'))

    def test_catches_patch_object_on_bare_os_path(self):
        self.assertTrue(_PATCH_OBJECT.search(
            'mock.patch.object(os.path, "exists", lambda p: True)'))

    def test_catches_the_string_form(self):
        self.assertTrue(_PATCH_STRING.search(
            'patch("cli.commands.box._ssh.os.path.exists", return_value=True)'))
        self.assertTrue(_PATCH_STRING.search(
            "mock.patch('os.path.exists', return_value=False)"))

    def test_does_not_flag_lookalike_names(self):
        # `myos.path` is somebody's own object; patching it really is scoped.
        self.assertFalse(_PATCH_OBJECT.search(
            'patch.object(myos.path, "exists", return_value=True)'))
        self.assertFalse(_PATCH_STRING.search(
            'patch("pkg.myos.path.exists", return_value=True)'))

    def test_does_not_flag_seam_patches(self):
        self.assertFalse(_PATCH_OBJECT.search(
            'mock.patch.object(_ssh, "lager_box_key_if_present", fake)'))
        self.assertFalse(_PATCH_STRING.search(
            'patch("cli.commands.box._ssh.lager_box_key_if_present")'))


if __name__ == "__main__":
    unittest.main()
