# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Pins the /etc/sudoers.d/ ownership contract described in
`cli/commands/box/_host_ops.py`.

This is not a fix for a bug — the invariant already holds. Lager writes three
fixed paths under /etc/sudoers.d/, always via `tee`, and removes only those
same three by name; no glob, no directory-level operation, and nothing that
reads or edits a file it did not write. An operator's own file there is safe.

What was missing was any way for an operator to KNOW that, and any way for the
tree to keep it true. A grant added inside one of Lager's three files is lost
on the next run, because those files are regenerated wholesale by design.
Every writer now emits an ownership banner saying so, and these tests pin both
halves: the three-path allowlist, and the banner reaching every file Lager
writes — including the copy in setup_and_deploy_box.sh, which runs from shell
and cannot import the constant.
"""

import importlib
import pathlib
import re
import unittest

ops = importlib.import_module("cli.commands.box._host_ops")
dut = importlib.import_module("cli.commands.box.dut")
mp = importlib.import_module("cli.commands.box._mount_prep")
uninstall = importlib.import_module("cli.commands.utility.uninstall")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEPLOY_SCRIPT = REPO_ROOT / "cli" / "deployment" / "scripts" / "setup_and_deploy_box.sh"

# The complete set of files Lager may write, remove, or otherwise touch under
# /etc/sudoers.d/. Adding an entry here is a deliberate act: it widens what
# `lager uninstall` deletes from an operator's box.
OWNED_SUDOERS_FILES = {
    "lagerdata-udev",
    "lager-box-config",
    "lager-bench-json",
}

_SUDOERS_REF = re.compile(r"/etc/sudoers\.d/([A-Za-z0-9_.*?\[\]-]*)")


def _source_files():
    """Every shipped .py/.sh under cli/ and box/ — the two trees that can
    reach a box. Tests and docs are excluded: they quote these paths to
    describe them, which is not the same as writing them."""
    for tree in ("cli", "box"):
        root = REPO_ROOT / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix in (".py", ".sh") and path.is_file():
                yield path


def _code_lines_touching_sudoers_d():
    """(path, lineno, line) for every non-commentary line that names
    /etc/sudoers.d/.

    A line is treated as commentary from its first `#` onward — which covers
    both real comments and the banner strings, whose content is itself a
    sudoers comment. The banner names /etc/sudoers.d/00-local as the example
    of a file Lager leaves alone; without this the allowlist test would read
    that prose as a claim of ownership.
    """
    for path in _source_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            hash_at = line.find("#")
            code = line if hash_at < 0 else line[:hash_at]
            if "/etc/sudoers.d/" in code:
                yield path.relative_to(REPO_ROOT), lineno, code


class ThreePathAllowlist(unittest.TestCase):
    def test_no_other_sudoers_file_is_named_in_code(self):
        found = {}
        for relpath, lineno, code in _code_lines_touching_sudoers_d():
            for name in _SUDOERS_REF.findall(code):
                if name:
                    found.setdefault(name, f"{relpath}:{lineno}")
        unexpected = {n: w for n, w in found.items() if n not in OWNED_SUDOERS_FILES}
        self.assertEqual(
            unexpected, {},
            "code names a /etc/sudoers.d/ file outside the ownership contract",
        )

    def test_every_owned_file_is_actually_referenced(self):
        # Guards the other direction: a name left in OWNED_SUDOERS_FILES after
        # its writer is deleted would keep `lager uninstall` removing a file
        # Lager no longer creates.
        referenced = set()
        for _relpath, _lineno, code in _code_lines_touching_sudoers_d():
            referenced.update(n for n in _SUDOERS_REF.findall(code) if n)
        self.assertEqual(OWNED_SUDOERS_FILES - referenced, set())

    def test_no_glob_targets_a_sudoers_path(self):
        # A glob is what turns "rewrite our own file" into "rewrite whatever
        # is in the directory". None exists today; keep it that way.
        for relpath, lineno, code in _code_lines_touching_sudoers_d():
            for name in _SUDOERS_REF.findall(code):
                self.assertNotIn("*", name, f"{relpath}:{lineno}")
                self.assertNotIn("?", name, f"{relpath}:{lineno}")
                self.assertNotIn("[", name, f"{relpath}:{lineno}")

    def test_no_directory_level_operations(self):
        # /etc/sudoers.d/ itself is never created, moved, emptied, recursed
        # over, or edited in place — only individual owned files are written.
        forbidden = [
            "rm -r", "rm -f -r", "rmdir", "mkdir", "mv ", "sed -i",
            "truncate", "shred", "find ", "chmod -R", "chown -R", "cp -r",
        ]
        for relpath, lineno, code in _code_lines_touching_sudoers_d():
            for verb in forbidden:
                self.assertNotIn(
                    verb, code, f"{relpath}:{lineno} operates on the directory",
                )

    def test_uninstall_removes_exactly_the_owned_files(self):
        step = {n: c for n, _d, c in uninstall.UNINSTALL_ALL_PRIV_STEPS}["sudoers"]
        removed = set(_SUDOERS_REF.findall(step))
        self.assertEqual(removed, OWNED_SUDOERS_FILES)
        # `rm -f` (not -r) so a directory can never be the target.
        self.assertIn("rm -f", step)
        self.assertNotIn("rm -rf", step)


class BannerReachesEveryWriter(unittest.TestCase):
    def test_boxcfg_bootstrap_cmd_leads_with_the_banner(self):
        cmd = ops.boxcfg_sudoers_bootstrap_cmd("benchtest")
        quoted_banner = " ".join(f"'{line}'" for line in ops.BOXCFG_SUDOERS_BANNER)
        self.assertIn(f"printf '%s\\n' {quoted_banner} 'benchtest ", cmd)

    def test_manual_boxcfg_snippet_matches_the_bootstrap_content(self):
        # An operator who pastes the snippet must end up with the same file
        # install/update write — grants AND banner.
        text = ops.sudoers_bootstrap("benchtest")
        for line in ops.BOXCFG_SUDOERS_BANNER:
            self.assertIn(f"'{line}'", text)

    def test_udev_manual_snippet_carries_the_banner(self):
        text = ops.udev_sudoers_bootstrap("benchtest")
        for line in ops.UDEV_SUDOERS_BANNER:
            self.assertIn(line, text)

    def test_bench_json_manual_snippet_carries_the_banner(self):
        text = dut._bench_sudoers_bootstrap("benchtest")
        for line in dut._BENCH_SUDOERS_BANNER:
            self.assertIn(line, text)

    def test_mount_prep_writes_the_managed_content_not_a_subset(self):
        # This snippet used to tee a strict subset (mkdir + chown alone) over
        # the same managed path, so pasting it dropped the apt-get/sysctl/cp
        # grants and wrote no marker.
        text = mp.sudoers_bootstrap("benchtest")
        self.assertEqual(text, ops.sudoers_bootstrap("benchtest"))
        self.assertIn("NOPASSWD: /bin/mkdir, /bin/chown", text)
        self.assertIn("/usr/bin/apt-get", text)


class BannerIsSafeToEmit(unittest.TestCase):
    ALL_BANNERS = None

    def setUp(self):
        self.ALL_BANNERS = [
            ops.BOXCFG_SUDOERS_BANNER,
            ops.UDEV_SUDOERS_BANNER,
            dut._BENCH_SUDOERS_BANNER,
        ]

    def test_every_line_is_a_sudoers_comment(self):
        # `visudo -c` must still pass; a non-comment line would be parsed as
        # a rule.
        for banner in self.ALL_BANNERS:
            for line in banner:
                self.assertTrue(line.startswith("# "), line)

    def test_no_quoting_hazards(self):
        # Each line is wrapped in shell single quotes by the printf writer,
        # and setup_and_deploy_box.sh emits its copy from an *unquoted*
        # heredoc where backticks and $ would expand client-side.
        for banner in self.ALL_BANNERS:
            for line in banner:
                self.assertNotIn("'", line)
                self.assertNotIn("`", line)
                self.assertNotIn("$", line)
                self.assertTrue(line.isascii(), line)

    def test_banner_states_the_contract(self):
        # The point of the banner is these two facts. Reword freely, but a
        # banner that stops saying them stops preventing the incident.
        for banner in self.ALL_BANNERS:
            text = " ".join(banner)
            self.assertIn("/etc/sudoers.d/", text)
            self.assertIn("SEPARATE file", text)

    def test_lifecycle_line_names_only_commands_that_write_the_file(self):
        # lagerdata-udev has no update-path writer: `lager update` only chowns
        # it to root:root. Claiming otherwise would send an operator to a
        # command that cannot deliver the change.
        self.assertNotIn("lager update", ops.UDEV_SUDOERS_BANNER[0])
        self.assertIn("lager install", ops.UDEV_SUDOERS_BANNER[0])
        self.assertIn("lager update", ops.BOXCFG_SUDOERS_BANNER[0])


class EscalationPostureIsStatedNotClaimedAway(unittest.TestCase):
    """The recorded posture: the box login user is root-equivalent by design,
    because provisioning needs root. These pin the statement of it, and pin
    that the old contrary claim does not come back.

    The tree used to assert the opposite in two places — the rules docstring
    and the operator-facing bootstrap text both said a compromised account
    "cannot escalate to root" via the path-scoped entries. That is true of
    those entries in isolation and false of the file, which grants apt-get
    one line above them.
    """

    def test_bootstrap_text_tells_the_operator_the_account_is_root(self):
        text = ops.sudoers_bootstrap("benchtest")
        self.assertIn("root-equivalent", text)
        self.assertIn("not a privilege boundary", text)

    def test_no_source_claims_the_account_cannot_escalate(self):
        offenders = []
        for path in _source_files():
            body = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(body.splitlines(), 1):
                if "cannot escalate" in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        self.assertEqual(
            offenders, [],
            "a scoped sudoers entry limits blast radius; it does not confine "
            "an account that can already reach root via apt-get and udev",
        )

    def test_rules_docstring_names_the_paths_to_root(self):
        # Narrowing mkdir/chown and stopping there produces a file that looks
        # hardened and is not. Whoever reads this function next should meet
        # the three real paths before they try.
        doc = ops.boxcfg_sudoers_rules.__doc__
        self.assertIn("ROOT-EQUIVALENT BY DESIGN", doc)
        for path_to_root in ("apt-get", "RUN+=", "/tmp"):
            self.assertIn(path_to_root, doc)


def _udev_heredoc_body():
    """The literal text setup_and_deploy_box.sh tees into lagerdata-udev,
    with ${BOX_USER} left unexpanded."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    body = re.search(
        r"sudo tee /etc/sudoers\.d/lagerdata-udev[^\n]*<< 'SUDOERS'\n(.*?)\nSUDOERS\n",
        text, re.DOTALL,
    )
    assert body, "could not locate the lagerdata-udev heredoc"
    return body.group(1)


class DeployScriptCopyStaysInSync(unittest.TestCase):
    def test_heredoc_opens_with_the_udev_banner_verbatim(self):
        body = _udev_heredoc_body()
        expected = "\n".join(ops.UDEV_SUDOERS_BANNER)
        self.assertTrue(
            body.startswith(expected),
            "setup_and_deploy_box.sh's banner has drifted from "
            "_host_ops.UDEV_SUDOERS_BANNER:\n" + body[:len(expected) + 80],
        )

    def test_heredoc_has_no_backticks(self):
        # The enclosing heredoc delimiter is unquoted (so ${BOX_USER} expands
        # client-side), which makes a backtick a command substitution rather
        # than a character written to the file.
        self.assertNotIn("`", _udev_heredoc_body())

    def test_heredoc_grants_only_the_expected_user(self):
        for line in _udev_heredoc_body().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            self.assertTrue(
                line.startswith("${BOX_USER} ALL=(ALL) NOPASSWD: "), line,
            )


class MarkerPathHasOneSource(unittest.TestCase):
    def test_no_stale_marker_literal_survives_a_bump(self):
        # install/update used to hardcode the marker path in their probes, so
        # bumping BOXCFG_SUDOERS_MARKER changed the file Lager wrote without
        # changing the file it checked for — the rewrite would be skipped on
        # every box forever. Every mention must now come from the constant.
        stale = []
        for path in _source_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                hash_at = line.find("#")
                code = line if hash_at < 0 else line[:hash_at]
                if ".boxcfg-sudoers-v" not in code:
                    continue
                if path.name == "_host_ops.py":
                    continue  # the definition itself
                stale.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        self.assertEqual(stale, [], "hardcoded marker path; import the constant")


if __name__ == "__main__":
    unittest.main()
