# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager install` and `lager update` with no `--version` deploy the release tag
of the CLI doing the deploy, not `main`.

Defaulting to `main` put every customer box on whatever had merged most
recently, and `lager hello` flagged a freshly installed box "not a release
build" even when `main` sat on the release commit.
"""
import ast
import importlib
import inspect
import re
import shlex
import subprocess
import unittest
from unittest import mock

from click.testing import CliRunner

import cli
from cli.commands.box import _ssh

install_mod = importlib.import_module("cli.commands.utility.install")
update_mod = importlib.import_module("cli.commands.utility.update")
bs = importlib.import_module("cli.box_storage")


class DefaultBoxVersion(unittest.TestCase):
    def test_it_is_this_clis_release_tag(self):
        self.assertEqual(update_mod.default_box_version(), f"v{cli.__version__}")

    def test_it_resolves_as_a_tag_not_a_branch(self):
        checkout, reset, fetch = update_mod.resolve_version_ref(
            update_mod.default_box_version())
        self.assertEqual(checkout, reset)
        self.assertTrue(fetch.startswith("refs/tags/"), fetch)

    def test_it_has_a_published_image(self):
        # Only release tags have one, so a default install can skip the build.
        self.assertTrue(update_mod._box_image_ref_for_version(
            update_mod.default_box_version()))


def _proc(rc, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


class InstallDefault(unittest.TestCase):
    """install.py over a faked transport, looking only at the deploy call."""

    def _install(self, extra_args):
        deploy_calls, written = [], {}

        def fake_run(cmd, **kw):
            cmd = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
            if cmd and cmd[0] != "ssh":
                deploy_calls.append(cmd)
                return _proc(0)
            remote = cmd[-1]
            if "show HEAD:cli/__init__.py" in remote:
                return _proc(0, f"__version__ = '{cli.__version__}'\n")
            target = re.search(r'mv -f "\$tmp" /etc/lager/([a-z-]+)', remote)
            if target:
                written[target.group(1)] = shlex.split(
                    remote.split("printf '%s\\n' ", 1)[1])[0]
                return _proc(0)
            if remote == "cat /etc/lager/version":
                return _proc(0, written.get("version", "") + "\n")
            return _proc(0, f"{cli.__version__}\n")

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
        result = CliRunner().invoke(
            install_mod.install, ["--ip", "10.0.0.1", "--yes"] + extra_args)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(deploy_calls), 1, result.output)
        cmd = deploy_calls[0]
        return result, cmd[cmd.index("--version") + 1]

    def test_no_version_deploys_this_clis_tag_and_says_so(self):
        result, deployed = self._install([])
        self.assertEqual(deployed, f"v{cli.__version__}")
        self.assertIn("No --version given", result.output)
        self.assertIn("--version main", result.output)
        # The box is already on this CLI's release; a bare update now is a no-op.
        self.assertIn("After you upgrade the CLI", result.output)

    def test_an_explicit_main_is_still_main_and_says_nothing(self):
        result, deployed = self._install(["--version", "main"])
        self.assertEqual(deployed, "main")
        self.assertNotIn("No --version given", result.output)
        self.assertIn("lager update --box [BOX_NAME] --version main", result.output)


class DeployedRefName(unittest.TestCase):
    def _read(self, rc, stdout):
        return update_mod._read_deployed_ref_name(lambda cmd: _proc(rc, stdout))

    def test_it_is_the_ref_half(self):
        self.assertEqual(self._read(0, "cf/my-branch@071be04\n"), "cf/my-branch")

    def test_banner_noise_is_skipped(self):
        self.assertEqual(self._read(0, "Welcome!\n\nv0.52.0@b73aa66\n"), "v0.52.0")

    def test_no_file_is_empty(self):
        self.assertEqual(self._read(1, ""), "")
        self.assertEqual(self._read(0, ""), "")


class UpdateDefault(unittest.TestCase):
    @staticmethod
    def _update_logic():
        src = inspect.getsource(update_mod)
        tree = ast.parse(src)
        return next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_update_logic"
        )

    def test_the_target_defaults_to_this_clis_tag(self):
        logic = self._update_logic()
        assigns = [
            ast.unparse(node.value) for node in ast.walk(logic)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "target_version"
                    for t in node.targets)
        ]
        self.assertIn("version or default_box_version()", assigns)

    def test_a_defaulted_target_stops_before_the_rollback_prompt(self):
        """A bare `lager update --yes` must never downgrade a box.

        `--yes` skips the rollback confirmation, so the defaulted-target stop
        has to come before it -- otherwise a script on a box ahead of this
        CLI's release (a newer release, or a branch) rolls it back silently.
        """
        logic = self._update_logic()
        guard = next(
            node.lineno for node in ast.walk(logic)
            if isinstance(node, ast.If)
            and "version_defaulted" in ast.unparse(node.test)
            and "is_rollback" in ast.unparse(node.test)
        )
        prompt = next(
            node.lineno for node in ast.walk(logic)
            if isinstance(node, ast.Call)
            and "ROLL BACK" in ast.unparse(node)
            and ast.unparse(node.func).endswith("confirm")
        )
        self.assertLess(guard, prompt)


if __name__ == "__main__":
    unittest.main()
