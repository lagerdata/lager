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
        rules = "\n".join(_rule_lines(_udev_heredoc_body()))
        for needed in (
            "/bin/chmod 2775 /etc/lager",              # this script + update.py
            "/bin/chmod 755 /etc/lager",               # convert_to_sparse_checkout
            "/bin/chmod 644 /etc/lager/saved_nets.json",
            "/bin/chown 33:33 /etc/lager/saved_nets.json",
            "/bin/chmod 666 /etc/lager/version",       # install.py
        ):
            self.assertIn(needed, rules, f"missing grant: {needed}")

    def test_the_gid_grant_is_resolved_on_the_box(self):
        # `id -g` cannot be answered client-side; the dynamic block is why it
        # is a separate, unquoted heredoc.
        # Escaped in the source (\\${BOX_GID}) so it survives the OUTER,
        # unquoted SCRIPT_EOF heredoc and reaches the box-side script intact;
        # the inner SUDOERS_DYNAMIC heredoc is unquoted, which is what finally
        # expands it against the box's own `id -g`.
        dynamic = _udev_dynamic_body()
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
