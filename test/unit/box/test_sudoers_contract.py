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
import shutil
import unittest

ops = importlib.import_module("cli.commands.box._host_ops")
dut = importlib.import_module("cli.commands.box.dut")
mp = importlib.import_module("cli.commands.box._mount_prep")
uninstall = importlib.import_module("cli.commands.utility.uninstall")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEPLOY_SCRIPT = REPO_ROOT / "cli" / "deployment" / "scripts" / "setup_and_deploy_box.sh"

# Real visudo, not a stand-in: see GeneratedSudoersActuallyParses below.
_VISUDO = shutil.which("visudo") or (
    "/usr/sbin/visudo" if pathlib.Path("/usr/sbin/visudo").exists() else None
)

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
    """The literal text setup_and_deploy_box.sh writes for lagerdata-udev,
    with ${BOX_USER} left unexpanded.

    This is now staged to a temp file and installed only after `visudo -c -f`
    passes, rather than teed straight into /etc/sudoers.d (#313) -- so the
    anchor is the staging write, not the install."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    body = re.search(
        r"cat > \"\\?\$LAGER_SUDOERS_TMP\"[^\n]*<< 'SUDOERS'\n(.*?)\nSUDOERS\n",
        text, re.DOTALL,
    )
    assert body, "could not locate the lagerdata-udev heredoc"
    return body.group(1)


def _udev_dynamic_body():
    """The grants appended after the quoted heredoc, whose values can only be
    resolved on the box (the login user's gid)."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    body = re.search(
        r"cat >> \"\\?\$LAGER_SUDOERS_TMP\"[^\n]*<< SUDOERS_DYNAMIC\n(.*?)\nSUDOERS_DYNAMIC\n",
        text, re.DOTALL,
    )
    assert body, "could not locate the dynamic lagerdata-udev block"
    return body.group(1)


_SUDOERS_METACHARS = (":", ",", "=", "!")


def _unescape(text):
    """Drop sudoers backslash-escapes, leaving the command sudo will match.

    ':' , ',' , '=' and '!' separate entries in sudoers, so a command argument
    containing one is stored escaped. sudo unescapes before matching, so grant-
    matches-command assertions must compare the unescaped form."""
    for ch in _SUDOERS_METACHARS:
        text = text.replace("\\" + ch, ch)
    return text


def _rule_lines(body):
    """Just the NOPASSWD rules: comments and blank lines are not parsed by
    sudo, and a comment may legitimately discuss a wildcard."""
    return [ln for ln in body.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


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
        for line in _rule_lines(_udev_heredoc_body()):
            # The firewall grant is templated in from the client side, because
            # its argument (--corporate-vpn <iface>) is only known there; it
            # expands to a ${BOX_USER} rule or to nothing. See #313.
            if line.strip() == "${FIREWALL_SUDOERS_RULES}":
                continue
            self.assertTrue(
                line.startswith("${BOX_USER} ALL=(ALL) NOPASSWD: "), line,
            )

    def test_dynamic_block_grants_only_the_expected_user(self):
        for line in _rule_lines(_udev_dynamic_body()):
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


# --- #315: env through sudo -------------------------------------------------
#
# `sudo VAR=value cmd` only delivers VAR if the sudoers rule authorising `cmd`
# carries SETENV; with sudo's default env_reset it is otherwise dropped without
# a word. Lager has both shapes, and which one is correct depends entirely on
# whether a NOPASSWD grant is being relied on:
#
#   install  -- runs before any Lager sudoers file exists, under the operator's
#               own sudo rights, so nothing constrains WHICH binary may run.
#               `sudo env VAR=... apt-get` is correct: env sets the variables as
#               root and no sudoers policy is involved.
#   provisioned -- `_host_cli.HOST_VENV_APT_CMD` and `_host_ops` apt run under
#               `NOPASSWD: SETENV: /usr/bin/apt-get`. `sudo env` would run
#               /usr/bin/env as root instead, the rule would stop matching, and
#               `sudo -n` would be refused. These keep `sudo VAR=` -- and that
#               is the narrower grant, since permitting /usr/bin/env permits
#               every binary.
#
# Both halves are pinned here because each one silently breaks the other's
# call sites if it is applied uniformly.

_HOST_CLI = REPO_ROOT / "cli" / "commands" / "utility" / "_host_cli.py"
_HOST_OPS = REPO_ROOT / "cli" / "commands" / "box" / "_host_ops.py"

# `sudo` (optionally with flags) directly followed by VAR=value -- the shape
# that loses the assignment. `sudo env VAR=value` and `sudo -n env VAR=value`
# do not match.
_SUDO_BARE_ENV_ASSIGN = re.compile(
    r"sudo(?:\s+-[A-Za-z]+)*\s+[A-Z_][A-Z0-9_]*="
)


class EnvReachesAptDuringInstall(unittest.TestCase):
    """#315: the install path must not hand env to sudo on its command line."""

    def test_install_script_has_no_bare_sudo_assignment(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in text.splitlines()
            if not line.lstrip().startswith("#")
            and _SUDO_BARE_ENV_ASSIGN.search(line)
        ]
        self.assertEqual(
            offenders, [],
            "setup_and_deploy_box.sh runs apt before any Lager sudoers file "
            "exists, so `sudo VAR=value` is dropped by env_reset and "
            "needrestart runs anyway (#315). Use `sudo env VAR=value cmd`:\n"
            + "\n".join(offenders),
        )

    def test_install_script_still_suppresses_needrestart(self):
        # The fix must not be achieved by deleting the variables. Both are
        # load-bearing: DEBIAN_FRONTEND stops debconf prompting, and
        # NEEDRESTART_SUSPEND stops the post-invoke service-restart scan --
        # DEBIAN_FRONTEND does not reach needrestart.
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        wanted = "sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get"
        self.assertGreater(
            text.count(wanted), 0,
            "no `sudo env DEBIAN_FRONTEND=... NEEDRESTART_SUSPEND=... apt-get` "
            "remains in the install script",
        )

    def test_the_scan_would_catch_a_regression(self):
        # A tree-wide checker that silently matches nothing passes forever.
        self.assertIsNotNone(
            _SUDO_BARE_ENV_ASSIGN.search(
                "sudo DEBIAN_FRONTEND=noninteractive apt-get update"
            ),
        )
        self.assertIsNotNone(
            _SUDO_BARE_ENV_ASSIGN.search(
                "sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y x"
            ),
        )
        # ...and does not fire on the corrected shape.
        self.assertIsNone(
            _SUDO_BARE_ENV_ASSIGN.search(
                "sudo env DEBIAN_FRONTEND=noninteractive apt-get update"
            ),
        )


class GrantBackedAptKeepsTheSetenvShape(unittest.TestCase):
    """The other half: `sudo env` must NOT spread to the grant-backed sites."""

    def test_host_cli_venv_cmd_passes_env_through_sudo_directly(self):
        text = _HOST_CLI.read_text(encoding="utf-8")
        self.assertIn(
            "sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get",
            text,
            "HOST_VENV_APT_CMD relies on `NOPASSWD: SETENV: /usr/bin/apt-get`. "
            "Under `sudo env` the command run as root is /usr/bin/env, the rule "
            "stops matching, and a provisioned box starts prompting.",
        )
        self.assertNotIn("sudo env ", text)

    def test_host_ops_apt_passes_env_through_sudo_directly(self):
        text = _HOST_OPS.read_text(encoding="utf-8")
        self.assertIn(
            "sudo -n DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get",
            text,
        )
        self.assertNotIn("sudo -n env ", text)

    def test_the_grant_those_two_rely_on_still_names_apt_get_with_setenv(self):
        # If this rule ever loses SETENV, or names a different binary, the two
        # call sites above become the broken shape and this file should say so
        # rather than letting them fail on a box.
        rules = ops.boxcfg_sudoers_rules("lagerdata")
        self.assertTrue(
            any("NOPASSWD: SETENV: /usr/bin/apt-get" in r for r in rules),
            f"the SETENV apt-get grant is gone; rules are: {rules}",
        )

    def test_no_grant_permits_running_env_as_root(self):
        # Granting /usr/bin/env would make every narrow grant meaningless --
        # env runs any binary. Stated as a rule so nobody "fixes" the two call
        # sites above by widening the grant instead.
        rules = ops.boxcfg_sudoers_rules("lagerdata")
        for rule in rules:
            self.assertNotIn("/usr/bin/env", rule)
            self.assertNotIn("/bin/env", rule)


# --- #313: sudo-rs rejects wildcards in command arguments -------------------
#
# sudo-rs is the default sudo on Ubuntu 25.10 and 26.04 LTS, and it refuses
# `*` inside a command's arguments by design -- not as a missing feature, so
# the file will not become valid by waiting. `lager install` writes this file
# in step 2 of 9, so a single wildcard rule aborts the whole install before
# anything is deployed.
#
# This cannot be caught on hardware: every box we can reach carries a blanket
# `(ALL) NOPASSWD: ALL`, so the narrow grants are never exercised and a broken
# one is invisible to a green bench run. These assertions are the only thing
# standing between a wildcard and a customer's install.

class UdevSudoersIsWildcardFree(unittest.TestCase):
    def test_no_rule_uses_a_wildcard(self):
        offenders = [ln for ln in _rule_lines(_udev_heredoc_body()) if "*" in ln]
        self.assertEqual(
            offenders, [],
            "sudo-rs rejects wildcards in command arguments and refuses the "
            "whole file, so `lager install` dies in step 2 on Ubuntu 25.10+ "
            "(#313). Grant the concrete values instead:\n"
            + "\n".join(offenders),
        )

    def test_no_dynamic_rule_uses_a_wildcard(self):
        offenders = [ln for ln in _rule_lines(_udev_dynamic_body()) if "*" in ln]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_scan_sees_actual_rules(self):
        # A checker that silently matches nothing passes forever. Both blocks
        # must be non-empty, and the main one is the bulk of the file.
        self.assertGreater(len(_rule_lines(_udev_heredoc_body())), 20)
        self.assertGreater(len(_rule_lines(_udev_dynamic_body())), 0)

    def test_a_wildcard_would_be_caught(self):
        # Prove the predicate, not just the current state.
        fake = "${BOX_USER} ALL=(ALL) NOPASSWD: /bin/chmod * /etc/lager"
        self.assertIn("*", fake)
        self.assertEqual([ln for ln in _rule_lines(fake) if "*" in ln], [fake])


class UdevSudoersGrantsWhatIsActuallyCalled(unittest.TestCase):
    """Wildcard-free is only correct if the literals cover the real calls."""

    def test_staged_rule_files_are_granted_by_name(self):
        rules = "\n".join(_rule_lines(_udev_heredoc_body()))
        # UDEV_RULES_FILENAME is the one _host_ops stages; 99-instrument.rules
        # is the one this script ships. Both must be copyable.
        self.assertIn("/bin/cp /tmp/99-instrument.rules /etc/udev/rules.d/", rules)
        self.assertIn(
            f"/bin/cp /tmp/{ops.UDEV_RULES_FILENAME} /etc/udev/rules.d/", rules,
            "the rules file _host_ops stages is not granted by name",
        )

    def test_the_modes_and_owners_the_tree_applies_are_granted(self):
        # Compared UNESCAPED. The backslashes in the file are sudoers file
        # syntax; sudo strips them before matching, so the thing that has to
        # equal the CLI's command line is the unescaped rule, not the stored
        # bytes. Asserting the stored form would pin the escaping and stop
        # checking the grant.
        rules = _unescape("\n".join(_rule_lines(_udev_heredoc_body())))
        for needed in (
            "/bin/chmod 2775 /etc/lager",              # this script + update.py
            "/bin/chmod 755 /etc/lager",               # convert_to_sparse_checkout
            "/bin/chmod 644 /etc/lager/saved_nets.json",
            "/bin/chown 33:33 /etc/lager/saved_nets.json",
        ):
            self.assertIn(needed, rules, f"missing grant: {needed}")

    def test_the_version_and_ref_files_are_not_granted_at_all(self):
        # They are written without sudo (a mktemp file inside /etc/lager, then
        # `mv -f` over the target), so every grant that used to back the old
        # /tmp staging is gone. Pinned as an absence: an unused NOPASSWD rule
        # widens what the login user can do as root for no benefit, and these
        # in particular name files that decide what `lager hello` reports.
        rules = _unescape("\n".join(_rule_lines(_udev_heredoc_body())))
        for gone in (
            "/etc/lager/version",
            "/etc/lager/ref",
            "/tmp/lager_version_tmp",
            "/tmp/lager_ref_tmp",
        ):
            self.assertNotIn(gone, rules, f"grant should be gone: {gone}")

    def test_the_gid_grant_is_resolved_on_the_box(self):
        # `id -g` cannot be answered client-side; the dynamic block is why it
        # is a separate, unquoted heredoc.
        # Escaped in the source (\\${BOX_GID}) so it survives the OUTER,
        # unquoted SCRIPT_EOF heredoc and reaches the box-side script intact;
        # the inner SUDOERS_DYNAMIC heredoc is unquoted, which is what finally
        # expands it against the box's own `id -g`.
        dynamic = _unescape(_udev_dynamic_body())
        self.assertIn(r"/bin/chown 33:\${BOX_GID} /etc/lager", dynamic)
        self.assertIn(r"/usr/bin/chown 33:\${BOX_GID} /etc/lager", dynamic)

    def test_the_firewall_argument_form_is_templated_not_wildcarded(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("FIREWALL_SUDOERS_RULES=", text)
        self.assertIn("--corporate-vpn ${CORPORATE_VPN}", text)
        self.assertNotIn("secure_box_firewall.sh *", text)


class SudoersIsValidatedBeforeItIsInstalled(unittest.TestCase):
    """A file that fails validation must never reach /etc/sudoers.d."""

    def test_validation_targets_the_staged_file(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('visudo -c -f "\\$LAGER_SUDOERS_TMP"', text)

    def test_install_happens_only_on_the_success_branch(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        check = text.index('visudo -c -f "\\$LAGER_SUDOERS_TMP"')
        install = text.index(
            'install -m 0440 -o root -g root "\\$LAGER_SUDOERS_TMP" '
            '/etc/sudoers.d/lagerdata-udev'
        )
        self.assertLess(
            check, install,
            "the sudoers file is installed before it is validated -- a failing "
            "check then leaves the box with a BROKEN /etc/sudoers.d (#313)",
        )

    def test_a_sudo_rs_box_gets_an_actionable_message(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("sudo-rs", text)
        self.assertIn("update-alternatives --set sudo /usr/bin/sudo.ws", text)


class OperatorPasteTextIsWildcardFreeAndCorrect(unittest.TestCase):
    """The manual-fix text is a sudoers file an operator pastes as root.

    It taught the same globbed grants the install script used to write, so on
    a sudo-rs box it handed the operator a file their own visudo would reject
    -- on exactly the newer Ubuntu where they are most likely to hit the error
    that prints it (#313)."""

    def _paste_rules(self):
        text = ops.udev_sudoers_bootstrap("benchtest")
        return [ln.strip() for ln in text.splitlines() if "NOPASSWD" in ln]

    def test_no_wildcards(self):
        offenders = [ln for ln in self._paste_rules() if "*" in ln]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_it_grants_exactly_what_udev_apply_runs(self):
        # Wildcard-free is only an improvement if the literals still match.
        # sudo compares the command line verbatim, so a trailing-slash or
        # quoting difference here is a silent "a password is required".
        import shlex
        rules = self._paste_rules()
        for real in (
            f"/bin/cp {shlex.quote(ops._UDEV_TMP_PATH)} "
            f"{shlex.quote(ops.UDEV_RULES_DIR)}",
            f"/bin/chmod 644 {shlex.quote(ops.UDEV_RULES_PATH)}",
        ):
            self.assertTrue(
                any(real in ln for ln in rules),
                f"udev_apply runs `sudo -n {real}` but the paste text does not "
                f"grant it; rules are:\n" + "\n".join(rules),
            )

    def test_the_scan_sees_rules(self):
        self.assertGreaterEqual(len(self._paste_rules()), 4)


# --- The file has to PARSE, not merely avoid wildcards ---------------------
#
# #313 was fixed by replacing 19 wildcard rules with literal ones, and the
# literals introduced a different syntax error: ':' separates Cmnd_Spec entries
# in sudoers, so `chown 33:33 /path` ends the command spec mid-argument and
# visudo refuses the whole file. Every install then died at step 2 -- a wider
# break than the sudo-rs bug it was fixing, because it hit every box.
#
# Nothing caught it. The wildcard scan passed (there is no wildcard), the unit
# suite passed, and the render harness "validated" the output against a visudo
# STUB that returned 0 unconditionally. A check that cannot fail is not a check.
#
# So: run the real visudo. `visudo -c -f <file>` needs no privileges and exits
# 1 on a syntax error, which is the entire contract this file depends on.

def _assembled_sudoers(user="benchtest", gid="1000", vpn_iface=None):
    """The file as it reaches the box, with the templated values resolved."""
    static = _udev_heredoc_body().replace("${BOX_USER}", user)
    firewall = (
        f"{user} ALL=(ALL) NOPASSWD: /usr/local/lib/lager/secure_box_firewall.sh "
        f"--corporate-vpn {vpn_iface}"
        if vpn_iface else ""
    )
    static = static.replace("${FIREWALL_SUDOERS_RULES}", firewall)
    dynamic = (
        _udev_dynamic_body()
        .replace("${BOX_USER}", user)
        .replace(r"\${BOX_GID}", gid)
    )
    return static + "\n" + dynamic + "\n"


class GeneratedSudoersActuallyParses(unittest.TestCase):
    def _run_visudo(self, text):
        import subprocess, tempfile, os
        fd, path = tempfile.mkstemp(suffix=".sudoers")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            proc = subprocess.run(
                [_VISUDO, "-c", "-f", path],
                capture_output=True, text=True, timeout=30,
            )
            return proc.returncode, (proc.stdout + proc.stderr)
        finally:
            os.unlink(path)

    @unittest.skipUnless(_VISUDO, "visudo not available on this machine")
    def test_it_parses_with_no_corporate_vpn(self):
        rc, out = self._run_visudo(_assembled_sudoers())
        self.assertEqual(rc, 0, f"visudo rejected the generated sudoers:\n{out}")

    @unittest.skipUnless(_VISUDO, "visudo not available on this machine")
    def test_it_parses_with_a_corporate_vpn_interface(self):
        rc, out = self._run_visudo(_assembled_sudoers(vpn_iface="tun0"))
        self.assertEqual(rc, 0, f"visudo rejected the generated sudoers:\n{out}")

    @unittest.skipUnless(_VISUDO, "visudo not available on this machine")
    def test_visudo_actually_rejects_a_bad_file(self):
        # The guard on the guard. If visudo were stubbed, aliased or otherwise
        # toothless, the two tests above would pass on anything -- which is
        # exactly how the unescaped colon reached a box.
        bad = "benchtest ALL=(ALL) NOPASSWD: /bin/chown 33:33 /etc/lager/x\n"
        rc, _ = self._run_visudo(bad)
        self.assertEqual(rc, 1, "visudo accepted a known-invalid file")


class CommandArgumentsEscapeSudoersMetacharacters(unittest.TestCase):
    """Always-on companion: catches the same class without needing visudo."""

    def _command_parts(self):
        rules = _rule_lines(_udev_heredoc_body()) + _rule_lines(_udev_dynamic_body())
        parts = []
        for rule in rules:
            if rule.strip() == "${FIREWALL_SUDOERS_RULES}":
                continue
            _, _, cmd = rule.partition("NOPASSWD: ")
            if cmd:
                parts.append((rule, cmd))
        return parts

    def test_no_unescaped_metacharacter_in_a_command_argument(self):
        offenders = []
        for rule, cmd in self._command_parts():
            stripped = cmd.replace("\\:", "").replace("\\,", "")
            stripped = stripped.replace("\\=", "").replace("\\!", "")
            if any(ch in stripped for ch in _SUDOERS_METACHARS):
                offenders.append(rule)
        self.assertEqual(
            offenders, [],
            "these characters separate sudoers entries and must be backslash-"
            "escaped inside a command argument, or visudo refuses the whole "
            "file and every install dies at step 2:\n" + "\n".join(offenders),
        )

    def test_the_scan_sees_commands(self):
        self.assertGreater(len(self._command_parts()), 20)

    def test_an_unescaped_colon_would_be_caught(self):
        bad = "${BOX_USER} ALL=(ALL) NOPASSWD: /bin/chown 33:33 /etc/lager"
        _, _, cmd = bad.partition("NOPASSWD: ")
        self.assertTrue(any(ch in cmd for ch in _SUDOERS_METACHARS))


# ---------------------------------------------------------------------------
# One sudo session on a fresh box, and none on a re-install
# ---------------------------------------------------------------------------
#
# `lager install` asked for the sudo password several times: the sudo step ran
# unconditionally, two later steps ran commands no rule granted (`sudo find`,
# a recursive `sudo chown`), and install.py wrote the box-config file in a
# session of its own at the end. The fix is one session that does everything,
# and a check, asked with no terminal, that skips it when the box is current.
#
# A shell script proves nothing by being read, so most of what follows RUNS
# the extracted shell under bash.

DEPLOYMENT_DIR = REPO_ROOT / "cli" / "deployment"
_BASH = shutil.which("bash") or "/bin/bash"


def _extract_block(topic):
    """The shell between the BEGIN/END sentinels naming `topic`."""
    begin, end = f"# --- BEGIN {topic}", f"# --- END {topic}"
    body, inside, seen = [], False, False
    for line in DEPLOY_SCRIPT.read_text(encoding="utf-8").splitlines():
        if line.startswith(begin):
            inside, seen = True, True
            continue
        if line.startswith(end):
            inside = False
            continue
        if inside:
            body.append(line)
    assert seen, f"sentinel {begin!r} not found in {DEPLOY_SCRIPT}"
    assert body, f"no shell extracted for {topic!r}"
    return "\n".join(body)


def _code(text):
    """Without comment lines: a comment may name the thing it forbids."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


_SUDO_FIND = re.compile(r"\bsudo(?:\s+-\S+)*\s+find\b")
_SUDO_RECURSIVE_CHOWN = re.compile(r"\bsudo(?:\s+-\S+)*\s+chown\s+(?:-\w*R\w*|--recursive)\b")


class NothingUngrantableRunsUnderSudo(unittest.TestCase):
    """`find -exec` is a root shell and a recursive chown cannot skip
    authorized_keys.d, so neither may ever be granted -- which means neither
    may be RUN, or the operator is asked for a password every time."""

    def _offenders(self, pattern):
        found = []
        for path in sorted(DEPLOYMENT_DIR.rglob("*.sh")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.lstrip().startswith("#") and pattern.search(line):
                    found.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
        return found

    def test_no_script_runs_find_under_sudo(self):
        self.assertEqual(self._offenders(_SUDO_FIND), [])

    def test_no_script_runs_a_recursive_chown_under_sudo(self):
        self.assertEqual(self._offenders(_SUDO_RECURSIVE_CHOWN), [])

    def test_the_scan_sees_shell_scripts(self):
        self.assertGreater(len(list(DEPLOYMENT_DIR.rglob("*.sh"))), 3)

    def test_both_shapes_would_be_caught(self):
        self.assertTrue(_SUDO_FIND.search("ssh_t x 'sudo find /etc/lager -exec chown 33 {} +'"))
        self.assertTrue(_SUDO_FIND.search("sudo -n find / -delete"))
        self.assertTrue(_SUDO_RECURSIVE_CHOWN.search('sudo chown -R 33:"$(id -g)" /etc/lager'))
        self.assertTrue(_SUDO_RECURSIVE_CHOWN.search("sudo -n chown -hR 33:33 /x"))
        self.assertFalse(_SUDO_RECURSIVE_CHOWN.search("sudo chown 33:33 /etc/lager"))


class TheEtcLagerHelperGrant(unittest.TestCase):
    @staticmethod
    def _grant():
        # Looked up when a test runs, not when the class is defined: a missing
        # constant then fails these tests, not the collection of the whole file.
        return "${BOX_USER} ALL=(ALL) NOPASSWD: " + ops.ETC_LAGER_PERMS_HELPER

    def test_it_is_granted_by_exact_path(self):
        self.assertIn(self._grant(), _rule_lines(_udev_heredoc_body()))

    def test_no_rule_installs_it(self):
        # The firewall script is installed under a NOPASSWD grant, and its own
        # comment concedes what that costs: the login user picks the content.
        # This helper is put in place only inside the password session.
        rules = _rule_lines(_udev_heredoc_body()) + _rule_lines(_udev_dynamic_body())
        name = pathlib.PurePosixPath(ops.ETC_LAGER_PERMS_HELPER).name
        installers = [r for r in rules if name in r and r != self._grant()]
        self.assertEqual(installers, [])

    def test_the_session_installs_it_root_owned_before_it_writes_any_grant(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        install = text.index(
            'sudo install -D -m 0755 -o root -g root "\\$BOOT_DIR/etc_lager_perms.sh"')
        first_grant_file = text.index('visudo -c -f "\\$LAGER_SUDOERS_TMP"')
        self.assertLess(install, first_grant_file)

    def test_the_script_and_host_ops_name_the_same_two_paths(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(f'ETC_LAGER_PERMS_HELPER="{ops.ETC_LAGER_PERMS_HELPER}"', text)
        self.assertIn(f'DEPLOY_SUDOERS_MARKER="{ops.DEPLOY_SUDOERS_MARKER}"', text)
        # One assignment each; everything else goes through the variable.
        self.assertEqual(text.count(ops.DEPLOY_SUDOERS_MARKER), 1)

    def test_the_marker_is_not_under_sudoers_d(self):
        # It would be a fourth file there, and the ownership contract is three.
        self.assertTrue(ops.DEPLOY_SUDOERS_MARKER.startswith("/etc/lager/."))

    def test_uninstall_removes_the_helper(self):
        joined = " ".join(cmd for _n, _d, cmd in uninstall.UNINSTALL_ALL_PRIV_STEPS)
        self.assertIn(f"rm -f {ops.ETC_LAGER_PERMS_HELPER}", joined)

    def test_the_helper_ships_beside_the_firewall_script(self):
        self.assertTrue((DEPLOYMENT_DIR / "security" / "etc_lager_perms.sh").is_file())
        self.assertIn('ETC_LAGER_PERMS_SRC="${SCRIPT_DIR}/../security/etc_lager_perms.sh"',
                      DEPLOY_SCRIPT.read_text(encoding="utf-8"))


class EverySystemctlTheDeployRunsIsGranted(unittest.TestCase):
    """`daemon-reload` and `restart docker.socket` were run and never granted,
    so a box whose Docker needed a restart asked for the password again."""

    # The verb, then its arguments. `(?!\d*>)` keeps a redirection's file
    # descriptor (`docker.socket 2>/dev/null`) from being read as an argument.
    _CALL = re.compile(r"\bsudo systemctl ((?:[a-z-]+)(?: (?!\d*>)[A-Za-z0-9_.@-]+)*)")

    def _calls(self):
        # Commands the script RUNS. A line that echoes advice to the operator
        # ("sudo systemctl status docker") runs nothing and needs no grant.
        ran = "\n".join(
            ln for ln in _code(DEPLOY_SCRIPT.read_text(encoding="utf-8")).splitlines()
            if not ln.lstrip().startswith("echo "))
        return sorted(set(self._CALL.findall(ran)))

    def test_each_one_has_a_rule_in_both_bin_directories(self):
        rules = _rule_lines(_udev_heredoc_body())
        for call in self._calls():
            for directory in ("/bin", "/usr/bin"):
                self.assertIn(
                    f"${{BOX_USER}} ALL=(ALL) NOPASSWD: {directory}/systemctl {call}", rules,
                    f"`sudo systemctl {call}` is run and not granted")

    def test_the_scan_sees_the_recovery_chain(self):
        calls = self._calls()
        for expected in ("daemon-reload", "restart docker.socket", "restart docker",
                         "reset-failed docker.service docker.socket", "enable docker"):
            self.assertIn(expected, calls)


class TheBoxConfigFileIsValidatedBeforeItIsInstalled(unittest.TestCase):
    def test_same_discipline_as_the_udev_file(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        check = text.index('visudo -c -f "\\$LAGER_BOXCFG_TMP"')
        install = text.index(
            'install -m 0440 -o root -g root "\\$LAGER_BOXCFG_TMP" '
            '/etc/sudoers.d/lager-box-config')
        self.assertLess(check, install)

    def test_its_heredoc_cannot_be_mistaken_for_the_udev_one(self):
        # _udev_heredoc_body() reads up to the first line that is exactly
        # SUDOERS. A second heredoc with that delimiter would be folded in.
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("<< 'BOXCFG_RULES_EOF'", text)
        self.assertEqual(len(re.findall(r"<< 'SUDOERS'\n", text)), 1)


class _RendersTheSession(unittest.TestCase):
    """Harness only: renders the sudo session script as the deploy script does."""

    def _render(self, user="benchtest", vpn="", content="", marker="", helper_dir=None):
        import subprocess
        import tempfile
        block = _extract_block("sudo session render")
        script_dir = helper_dir or str(DEPLOY_SCRIPT.parent)
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "render.sh"
            source.write_text(block, encoding="utf-8")
            driver = (
                "set -e\n"
                'print_warning() { echo "WARN: $*" >&2; }\n'
                'print_error() { echo "ERR: $*" >&2; }\n'
                f'. "{source}"\n'
                'echo "HAVE_BOXCFG=$HAVE_BOXCFG" >&2\n'
                'cat "$TEMP_SCRIPT"\n'
                'rm -f "$TEMP_SCRIPT"\n'
            )
            env = {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "BOX_USER": user, "CORPORATE_VPN": vpn, "SCRIPT_DIR": script_dir,
                "LAGER_BOXCFG_SUDOERS_CONTENT": content,
                "LAGER_BOXCFG_SUDOERS_MARKER": marker,
            }
            proc = subprocess.run([_BASH, "-c", driver], env=env,
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout, proc.stderr

    def _parses(self, rendered):
        import subprocess
        proc = subprocess.run([_BASH, "-n"], input=rendered,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TheSessionScriptRenders(_RendersTheSession):
    def test_standalone_it_writes_the_udev_file_alone_and_still_parses(self):
        rendered, log = self._render()
        self.assertIn("HAVE_BOXCFG=0", log)
        self._parses(rendered)
        self.assertIn('if [ "0" = "1" ]; then', rendered)

    def test_the_real_box_config_text_is_accepted_and_embedded_verbatim(self):
        # Ties the two halves together: what _host_ops renders must pass the
        # validator in the shell script, for every shape of user name.
        for user in ("benchtest", "lagerdata", "a.b-c_d", "Upper9"):
            content = ops.boxcfg_sudoers_content(user)
            rendered, log = self._render(user=user, content=content,
                                         marker=ops.BOXCFG_SUDOERS_MARKER)
            self.assertIn("HAVE_BOXCFG=1", log, user)
            self._parses(rendered)
            self.assertIn(content + "\nBOXCFG_RULES_EOF\n", rendered)
            self.assertIn(f'sudo touch "{ops.BOXCFG_SUDOERS_MARKER}"', rendered)

    def test_a_rule_for_anyone_else_is_refused(self):
        content = ops.boxcfg_sudoers_content("benchtest") + "\nmallory ALL=(ALL) NOPASSWD: ALL"
        rendered, log = self._render(content=content, marker=ops.BOXCFG_SUDOERS_MARKER)
        self.assertIn("HAVE_BOXCFG=0", log)
        self.assertIn("WARN:", log)
        self.assertNotIn("mallory", rendered)

    def test_a_rule_that_only_mentions_the_user_later_on_the_line_is_refused(self):
        content = "mallory ALL=(ALL) NOPASSWD: ALL # benchtest ALL=(root) NOPASSWD: x"
        rendered, log = self._render(content=content, marker=ops.BOXCFG_SUDOERS_MARKER)
        self.assertIn("HAVE_BOXCFG=0", log)
        self.assertNotIn("mallory", rendered)

    def test_a_rule_running_as_anyone_but_root_is_refused(self):
        content = "benchtest ALL=(ALL) NOPASSWD: /bin/true"
        _rendered, log = self._render(content=content, marker=ops.BOXCFG_SUDOERS_MARKER)
        self.assertIn("HAVE_BOXCFG=0", log)

    def test_a_marker_outside_etc_lager_is_refused(self):
        content = ops.boxcfg_sudoers_content("benchtest")
        for marker in ("/etc/sudoers.d/x", "/etc/lager/../shadow", "/etc/lager/.a b",
                       "/etc/lager/.x; rm -rf /", "", "/tmp/.marker"):
            rendered, log = self._render(content=content, marker=marker)
            self.assertIn("HAVE_BOXCFG=0", log, marker)
            self.assertNotIn("lager-box-config \\", rendered.split("BOXCFG_RULES_EOF")[0])

    def test_the_marker_is_recorded_last(self):
        rendered, _ = self._render(content=ops.boxcfg_sudoers_content("benchtest"),
                                   marker=ops.BOXCFG_SUDOERS_MARKER)
        recorded = rendered.index(f'mv -f "{ops.DEPLOY_SUDOERS_MARKER}.tmp"')
        for earlier in ("/etc/sudoers.d/lagerdata-udev", "/etc/sudoers.d/lager-box-config",
                        f"sudo {ops.ETC_LAGER_PERMS_HELPER}", "python3-venv", "docker-buildx"):
            self.assertLess(rendered.rindex(earlier), recorded, earlier)

    def test_the_session_has_no_bare_sudo_assignment(self):
        rendered, _ = self._render()
        self.assertEqual(
            [ln for ln in _code(rendered).splitlines() if _SUDO_BARE_ENV_ASSIGN.search(ln)], [])


class TheDigestChangesWithWhatWouldBeInstalled(_RendersTheSession):
    def _digest(self, rendered, helper_text=None):
        import subprocess
        import tempfile
        block = _extract_block("deploy sudoers digest")
        helper = DEPLOYMENT_DIR / "security" / "etc_lager_perms.sh"
        with tempfile.TemporaryDirectory() as tmp:
            session = pathlib.Path(tmp) / "session.sh"
            session.write_text(rendered, encoding="utf-8")
            helper_copy = pathlib.Path(tmp) / "helper.sh"
            helper_copy.write_text(
                helper.read_text(encoding="utf-8") if helper_text is None else helper_text,
                encoding="utf-8")
            proc = subprocess.run(
                [_BASH, "-c", f'{block}\ndeploy_sudoers_digest "{session}" "{helper_copy}"'],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        digest = proc.stdout.strip()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        return digest

    def test_the_same_inputs_give_the_same_digest(self):
        self.assertEqual(self._digest(self._render()[0]), self._digest(self._render()[0]))

    def test_each_of_these_changes_it(self):
        base = self._digest(self._render()[0])
        content = ops.boxcfg_sudoers_content("benchtest")
        changed = {
            "another login user": self._digest(self._render(user="someoneelse")[0]),
            "a corporate vpn interface": self._digest(self._render(vpn="tun0")[0]),
            "box-config rules present": self._digest(
                self._render(content=content, marker=ops.BOXCFG_SUDOERS_MARKER)[0]),
            "a changed helper": self._digest(self._render()[0], helper_text="#!/bin/sh\nexit 0\n"),
        }
        for what, digest in changed.items():
            self.assertNotEqual(digest, base, what)
        self.assertEqual(len(set(changed.values())), len(changed))

    def test_a_comment_in_the_session_script_does_not(self):
        rendered = self._render()[0]
        reworded = rendered.replace("#!/bin/bash\n", "#!/bin/bash\n# a reworded comment\n", 1)
        self.assertNotEqual(rendered, reworded)
        self.assertEqual(self._digest(rendered), self._digest(reworded))


class TheCheckThatSkipsTheSessionCannotPrompt(unittest.TestCase):
    def setUp(self):
        self.code = _code(_extract_block("deploy sudoers probe"))

    def test_it_has_no_terminal(self):
        self.assertIn("-o BatchMode=yes", self.code)
        self.assertNotIn("ssh_t", self.code)
        self.assertNotRegex(self.code, r"\bssh\b[^\n]*\s-t\b")

    def test_every_sudo_in_it_is_sudo_n(self):
        self.assertEqual(re.findall(r"\bsudo\b(?! -n\b)", self.code), [])
        self.assertGreaterEqual(len(re.findall(r"\bsudo -n\b", self.code)), 2)

    def test_it_does_not_ask_sudo_to_list(self):
        self.assertNotRegex(self.code, r"sudo(\s+-\S+)*\s+-l\b")

    def test_it_runs_the_helper_not_just_reads_the_marker(self):
        # A marker can outlive its grants: `uninstall --all --keep-config`
        # removes the sudoers files and leaves /etc/lager behind.
        self.assertIn("sudo -n ${ETC_LAGER_PERMS_HELPER}", self.code)
        self.assertIn("${DEPLOY_SUDOERS_MARKER}", self.code)

    def test_an_empty_digest_never_skips(self):
        self.assertIn("test -n '${DEPLOY_DIGEST}'", self.code)

    def test_the_session_runs_only_when_the_check_fails(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        asked = text.index("if deploy_sudoers_current; then")
        session = text.index('ssh_t "${BOX_USER}@${BOX_IP}" "bash ${BOOT_DIR}/setup_sudo.sh')
        self.assertLess(asked, session)

    def test_it_asks_what_lager_install_will_ask_later(self):
        install_py = (REPO_ROOT / "cli" / "commands" / "utility" / "install.py").read_text()
        self.assertIn("test -f {BOXCFG_SUDOERS_MARKER}", install_py)
        self.assertIn("test -f ${BOXCFG_SUDOERS_MARKER} && sudo -n /usr/bin/apt-get --version",
                      self.code)
