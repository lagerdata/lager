# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager install` hands the box-config sudoers file to the deploy script, so a
fresh box is asked for its sudo password once.

The deploy script opens one sudo session on the box. install.py used to open a
second, at the very end of the install, to write
/etc/sudoers.d/lager-box-config -- a second password prompt, and one that
landed after the operator had stopped watching. The text of that file has a
single source, `_host_ops`, which a shell script cannot import; so install.py
renders it and passes it in the deploy script's environment, and the deploy
script writes both files in its one session.

The shell side -- that the handed-over text is validated, staged, checked with
visudo and only then installed -- is pinned in test_sudoers_contract.py, which
runs that shell. What is pinned here is the Python side of the handoff, and
that install.py no longer opens a terminal when there is nothing to ask for.
"""
import importlib
import os
import re
import shlex
import subprocess
import unittest
from unittest import mock

from click.testing import CliRunner

from cli.commands.box import _ssh

install_mod = importlib.import_module("cli.commands.utility.install")
ops = importlib.import_module("cli.commands.box._host_ops")
bs = importlib.import_module("cli.box_storage")

CONTENT = "LAGER_BOXCFG_SUDOERS_CONTENT"
MARKER = "LAGER_BOXCFG_SUDOERS_MARKER"


class DeployEnv(unittest.TestCase):
    def test_it_carries_the_file_text_and_the_marker_for_the_install_user(self):
        env = install_mod._deploy_env("benchtest")
        self.assertEqual(env[CONTENT], ops.boxcfg_sudoers_content("benchtest"))
        self.assertEqual(env[MARKER], ops.BOXCFG_SUDOERS_MARKER)

    def test_the_text_is_exactly_what_the_other_writer_installs(self):
        # Two writers of one file. boxcfg_sudoers_bootstrap_cmd() is the other;
        # each of its lines is a single-quoted printf argument.
        cmd = ops.boxcfg_sudoers_bootstrap_cmd("benchtest")
        printf_args = shlex.split(cmd.split(" | sudo tee ", 1)[0])[2:]
        self.assertEqual("\n".join(printf_args), install_mod._deploy_env("benchtest")[CONTENT])

    def test_the_banner_comes_first_and_every_rule_names_the_user(self):
        lines = install_mod._deploy_env("benchtest")[CONTENT].splitlines()
        self.assertEqual(lines[:len(ops.BOXCFG_SUDOERS_BANNER)], ops.BOXCFG_SUDOERS_BANNER)
        rules = [ln for ln in lines if not ln.startswith("#")]
        self.assertEqual(rules, ops.boxcfg_sudoers_rules("benchtest"))
        for rule in rules:
            self.assertTrue(rule.startswith("benchtest ALL=(root) NOPASSWD: "), rule)

    def test_a_name_that_is_not_a_plain_unix_username_gets_neither(self):
        for user in ("bad user", "x;rm -rf /", "$(id)", "", "a\nb ALL=(ALL) NOPASSWD: ALL"):
            env = install_mod._deploy_env(user)
            self.assertNotIn(CONTENT, env, user)
            self.assertNotIn(MARKER, env, user)

    def test_a_stale_value_in_the_operators_shell_is_never_inherited(self):
        stale = {CONTENT: "mallory ALL=(ALL) NOPASSWD: ALL", MARKER: "/etc/sudoers.d/x"}
        with mock.patch.dict(os.environ, stale):
            invalid = install_mod._deploy_env("bad user")
            valid = install_mod._deploy_env("benchtest")
        self.assertNotIn(CONTENT, invalid)
        self.assertNotIn(MARKER, invalid)
        self.assertNotIn("mallory", valid[CONTENT])
        self.assertEqual(valid[MARKER], ops.BOXCFG_SUDOERS_MARKER)

    def test_the_rest_of_the_environment_is_kept_and_not_mutated(self):
        with mock.patch.dict(os.environ, {"PATH": "/opt/x:/usr/bin", "LAGER_TEST_KEEP": "1"}):
            before = dict(os.environ)
            env = install_mod._deploy_env("benchtest")
            self.assertEqual(env["PATH"], "/opt/x:/usr/bin")
            self.assertEqual(env["LAGER_TEST_KEEP"], "1")
            self.assertEqual(dict(os.environ), before)


def _proc(rc, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _answer_install_state(remote, written):
    """Answer install's post-deploy state calls the way a healthy box does:
    the version read from the checkout, the /etc/lager state writes, and the
    read-back. Returns None for any other command."""
    if "show HEAD:cli/__init__.py" in remote:
        return _proc(0, "__version__ = '0.36.2'\n")
    target = re.search(r'mv -f "\$tmp" /etc/lager/([a-z-]+)', remote)
    if target:
        written[target.group(1)] = shlex.split(remote.split("printf '%s\\n' ", 1)[1])[0]
        return _proc(0)
    if remote == "cat /etc/lager/version":
        return _proc(0, written.get("version", "") + "\n")
    return None


class TheInstallItself(unittest.TestCase):
    """install.py over a faked transport: the deploy call, and the step after."""

    def _install(self, *, box_config_grant_is_live, user=None):
        calls, kwargs_seen, written = [], [], {}

        def fake_run(cmd, **kw):
            cmd = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
            calls.append(cmd)
            kwargs_seen.append(kw)
            if cmd and cmd[0] != "ssh":
                return _proc(0)  # the deploy script
            if ops.BOXCFG_SUDOERS_MARKER in cmd[-1] and "-t" not in cmd:
                # install's own check of the box-config grant.
                return _proc(0 if box_config_grant_is_live else 1)
            answered = _answer_install_state(cmd[-1], written)
            return answered if answered is not None else _proc(0, "0.36.2\n")

        patches = [
            mock.patch.object(bs, "acquire_box_lock", return_value=("acquired", {})),
            mock.patch.object(bs, "release_box_lock", return_value=True),
            mock.patch.object(bs, "get_lock_holder", return_value="test-holder"),
            mock.patch.object(bs, "HeartbeatThread", return_value=mock.Mock()),
            mock.patch.object(install_mod.subprocess, "run", fake_run),
            mock.patch.object(_ssh, "lager_box_key_if_present", lambda *a, **k: None),
            mock.patch.object(install_mod, "lager_box_key_if_present", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        argv = ["--ip", "10.0.0.1", "--yes"] + (["--user", user] if user else [])
        result = CliRunner().invoke(install_mod.install, argv)
        deploy = [(c, k) for c, k in zip(calls, kwargs_seen) if c and c[0] != "ssh"]
        terminals = [c for c in calls if c and c[0] == "ssh" and "-t" in c]
        return result, deploy, terminals

    def test_the_deploy_script_is_run_with_the_handoff_in_its_environment(self):
        result, deploy, _ = self._install(box_config_grant_is_live=True, user="benchtest")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(deploy), 1)
        cmd, kwargs = deploy[0]
        self.assertTrue(cmd[0].endswith("setup_and_deploy_box.sh"), cmd)
        env = kwargs.get("env") or {}
        self.assertEqual(env.get(CONTENT), ops.boxcfg_sudoers_content("benchtest"))
        self.assertEqual(env.get(MARKER), ops.BOXCFG_SUDOERS_MARKER)
        self.assertIn("PATH", env)

    def test_no_terminal_is_opened_when_the_deploy_script_already_wrote_the_file(self):
        # The case this change exists for: fresh box or re-install, the grant
        # is live by the time install.py looks, so it asks for nothing.
        result, _, terminals = self._install(box_config_grant_is_live=True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(terminals, [])
        self.assertNotIn("asks for the sudo password", result.output)
        self.assertIn("already configured", result.output)

    def test_the_old_path_still_works_and_only_it_warns_of_a_prompt(self):
        # A deploy script run that could not write the file (an unusual user
        # name, a validation failure) leaves install.py's own step to do it.
        result, _, terminals = self._install(box_config_grant_is_live=False)
        self.assertEqual(len(terminals), 1, result.output)
        self.assertIn("/etc/sudoers.d/lager-box-config", terminals[0][-1])
        self.assertIn("asks for the sudo password", result.output)


if __name__ == "__main__":
    unittest.main()
