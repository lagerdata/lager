# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""`lager update --fork` and `lager install --fork`: deploy from a GitHub fork.

Testing box code used to mean pushing a branch to lagerdata/lager, which needs
write access. `--fork <owner>/<repo>` fetches the ref from a public fork instead.

The properties pinned here:

- The fork is fetched BY URL into ``refs/lager/fork/``, never through ``origin``
  and never as a named remote. A ``fork/main`` beside ``origin/main`` would make
  a later plain ``git checkout main`` ambiguous, and a fork tag fetched into
  ``refs/tags/`` would overwrite the real release tag.
- The fork fetch is unfiltered and tag-free, and cannot prompt for credentials.
- The value is validated, not escaped: it lands in a remote shell command.
- A fork never gets the GHCR image, and is never recorded as a release.
- Without ``--fork`` every generated command is unchanged.
- The deploy script's bash resolver agrees with the Python one.
"""
import importlib
import pathlib
import re
import shlex
import subprocess
import unittest
from unittest import mock

import click
import pytest
from click.testing import CliRunner

import cli
from cli.commands.box import _ssh
from cli.commands.box.hello import _ref_suffix

update_mod = importlib.import_module("cli.commands.utility.update")
install_mod = importlib.import_module("cli.commands.utility.install")
bs = importlib.import_module("cli.box_storage")

ROOT = pathlib.Path(__file__).resolve().parents[3]
DEPLOY_SCRIPT = ROOT / "cli" / "deployment" / "scripts" / "setup_and_deploy_box.sh"

SHA = "5d84c68612384eed2854638c1e0941a4ff8b7893"


class ValidateFork(unittest.TestCase):
    def test_owner_repo_is_accepted(self):
        for value, expected in [
            ("octocat/lager", ("octocat", "lager")),
            ("a/b", ("a", "b")),
            ("my-org/lager.fork_2", ("my-org", "lager.fork_2")),
        ]:
            self.assertEqual(update_mod.validate_fork(value), expected)

    def test_anything_else_is_refused(self):
        for value in [
            "", "lager", "octocat/", "/lager", "octocat/lager/extra",
            "https://github.com/octocat/lager", "git@github.com:octocat/lager",
            "octocat/lager;id", "octocat/lager $(id)", "octo cat/lager",
            "-octocat/lager", "octocat/..", "octocat/.", "octocat/lager\n",
            "a" * 40 + "/lager",
        ]:
            with self.assertRaises(click.BadParameter, msg=repr(value)):
                update_mod.validate_fork(value)

    def test_url_is_https_github(self):
        self.assertEqual(update_mod.fork_url("octocat", "lager"),
                         "https://github.com/octocat/lager.git")


class ResolveForkVersionRef(unittest.TestCase):
    def test_branch_goes_to_the_private_namespace(self):
        self.assertEqual(
            update_mod.resolve_fork_version_ref("de/my-branch"),
            ("de/my-branch", "refs/lager/fork/heads/de/my-branch",
             "+refs/heads/de/my-branch:refs/lager/fork/heads/de/my-branch"),
        )

    def test_a_fork_tag_never_lands_in_refs_tags(self):
        for version in ["v0.61.0", "0.61.0", "v1.2.3-rc1"]:
            display, ref, fetch = update_mod.resolve_fork_version_ref(version)
            self.assertTrue(display.startswith("v"))
            self.assertTrue(ref.startswith("refs/lager/fork/tags/"), ref)
            dst = fetch.split(":", 1)[1]
            self.assertFalse(dst.startswith("refs/tags/"), fetch)
            self.assertTrue(fetch.startswith("+"), "forced, like the origin tag fetch")

    def test_an_unfetchable_version_is_refused(self):
        for version in ["", "main;id", "a b", "$(id)", "main\n", "a..b", "x`id`"]:
            with self.assertRaises(click.BadParameter, msg=repr(version)):
                update_mod.resolve_fork_version_ref(version)

    def test_a_sha_resolves_to_itself(self):
        self.assertEqual(update_mod.resolve_fork_version_ref(SHA.upper()),
                         (SHA, SHA, SHA))

    def test_never_a_remote_tracking_ref(self):
        for version in ["main", "v0.61.0", SHA]:
            for part in update_mod.resolve_fork_version_ref(version):
                self.assertNotIn("refs/remotes/", part)
                self.assertFalse(part.startswith("origin/"), part)

    def test_ref_label(self):
        self.assertEqual(update_mod.fork_ref_label("o/lager", "main"), "o/lager:main")
        self.assertEqual(update_mod.fork_ref_label(None, "main"), "main")


class FetchAndPullScripts(unittest.TestCase):
    URL = "https://github.com/octocat/lager.git"

    def _fork_fetch(self, version="main"):
        _, ref, fetch = update_mod.resolve_fork_version_ref(version)
        return update_mod._fetch_shell_script(fetch, ref, source=self.URL), ref

    def test_fork_fetch_uses_the_url_not_origin(self):
        script, ref = self._fork_fetch()
        self.assertIn(f"fetch --force --no-tags {self.URL} ", script)
        self.assertNotIn("origin", script)
        self.assertIn(f"HEAD...{ref}", script)

    def test_fork_fetch_is_unfiltered_and_cannot_prompt(self):
        script, _ = self._fork_fetch()
        self.assertNotIn("--filter", script)
        self.assertIn("GIT_TERMINAL_PROMPT=0", script)

    def test_without_a_fork_the_fetch_is_unchanged(self):
        self.assertEqual(
            update_mod._fetch_shell_script("main", "origin/main"),
            'cd ~/box && git fetch origin --force main 2>&1; '
            'echo "LAGER_FETCH_RC=$?"; '
            'git rev-list --left-right --count HEAD...origin/main 2>/dev/null',
        )

    def test_fork_pull_checks_out_detached(self):
        script = update_mod._pull_shell_script(
            "main", "refs/lager/fork/heads/main", detach=True)
        self.assertTrue(script.endswith(
            "git checkout -f --detach refs/lager/fork/heads/main && "
            "git reset --hard refs/lager/fork/heads/main"), script)

    def test_without_a_fork_the_pull_is_unchanged(self):
        self.assertTrue(update_mod._pull_shell_script("main", "origin/main").endswith(
            "git checkout -f main && git reset --hard origin/main"))


class FetchErrorClassification(unittest.TestCase):
    def test_unreachable_fork(self):
        for stderr in [
            "remote: Repository not found.\nfatal: repository 'https://...' not found",
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        ]:
            self.assertTrue(update_mod._fork_unreachable(stderr), stderr)

    def test_missing_ref(self):
        self.assertFalse(update_mod._fork_unreachable(
            "fatal: couldn't find remote ref refs/heads/nope"))
        self.assertTrue(update_mod._fork_ref_missing(
            "fatal: couldn't find remote ref refs/heads/nope"))
        self.assertTrue(update_mod._fork_ref_missing(
            "error: Server does not allow request for unadvertised object " + SHA))


class UpdateCommandSurface(unittest.TestCase):
    """Rejections happen before any box lookup or SSH."""

    def _invoke(self, args):
        with mock.patch.object(update_mod, "get_default_box",
                               side_effect=AssertionError("reached box lookup")):
            return CliRunner().invoke(update_mod.update, args)

    def test_fork_requires_version(self):
        result = self._invoke(["--box", "X", "--fork", "octocat/lager"])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("--fork needs --version", result.output)

    def test_bad_fork_is_refused(self):
        result = self._invoke(["--box", "X", "--fork", "x/y;id", "--version", "main"])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("<owner>/<repo>", result.output)

    def test_unsafe_version_is_refused_under_fork(self):
        result = self._invoke(["--box", "X", "--fork", "octocat/lager",
                               "--version", "main;id"])
        self.assertEqual(result.exit_code, 2, result.output)

    def test_pull_and_fork_are_exclusive(self):
        result = self._invoke(["--box", "X", "--fork", "octocat/lager",
                               "--version", "v0.61.0", "--pull"])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("mutually exclusive", result.output)


def _proc(rc, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


class InstallFork(unittest.TestCase):
    """install.py over a faked transport, as in test_default_deploy_version."""

    def _install(self, extra_args, expect_exit=0):
        deploy_calls, written = [], {}

        def fake_run(cmd, **kw):
            cmd = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
            if cmd and cmd[0] != "ssh":
                deploy_calls.append(cmd)
                return _proc(0)
            remote = cmd[-1]
            if "show HEAD:cli/__init__.py" in remote:
                return _proc(0, "__version__ = '0.62.0'\n")
            if "rev-parse" in remote:
                return _proc(0, "abc1234\n")
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
        self.assertEqual(result.exit_code, expect_exit, result.output)
        return result, deploy_calls, written

    def test_fork_is_forwarded_and_the_image_is_never_pulled(self):
        result, calls, written = self._install(
            ["--fork", "octocat/lager", "--version", "v0.61.0"])
        self.assertEqual(len(calls), 1, result.output)
        cmd = calls[0]
        self.assertEqual(cmd[cmd.index("--fork") + 1], "octocat/lager")
        self.assertIn("--no-pull", cmd)
        self.assertNotIn("--pull", cmd)
        self.assertIn("Fork: octocat/lager", result.output)
        # A fork's v0.61.0 is not the release: version from the tree, ref labelled.
        self.assertTrue(written["version"].startswith("0.62.0|"), written)
        self.assertTrue(written["ref"].startswith("octocat/lager:v0.61.0"), written)
        self.assertIn("--fork octocat/lager --version v0.61.0", result.output)

    def test_fork_requires_version_before_any_deploy(self):
        result, calls, _ = self._install(["--fork", "octocat/lager"], expect_exit=2)
        self.assertEqual(calls, [])
        self.assertIn("--fork needs --version", result.output)

    def test_bad_fork_is_refused_before_any_deploy(self):
        result, calls, _ = self._install(
            ["--fork", "https://github.com/x/y", "--version", "main"], expect_exit=2)
        self.assertEqual(calls, [])

    def test_unsafe_version_is_refused_before_any_deploy(self):
        _, calls, _ = self._install(
            ["--fork", "octocat/lager", "--version", "main;id"], expect_exit=2)
        self.assertEqual(calls, [])

    def test_without_fork_nothing_is_forwarded(self):
        _, calls, written = self._install(["--version", "main"])
        self.assertNotIn("--fork", calls[0])
        self.assertTrue(written["ref"].startswith("main"), written)


class HelloRefSuffix(unittest.TestCase):
    def test_fork_build_is_flagged_even_on_a_release_shaped_ref(self):
        out = click.unstyle(_ref_suffix("octocat/lager:v0.61.0@abc1234", "0.61.0"))
        self.assertIn("fork build", out)
        self.assertNotIn("(release)", out)

    def test_existing_shapes_unchanged(self):
        self.assertEqual(_ref_suffix("v0.61.0@abc1234", "0.61.0"), " (release)")
        self.assertIn("not a release build", click.unstyle(_ref_suffix("main@abc1234", "0.61.0")))


# --- Deploy script: the bash fork block agrees with the Python resolver --------

def _extract(topic):
    begin, end = f"# --- BEGIN {topic}", f"# --- END {topic}"
    body, inside = [], False
    for line in DEPLOY_SCRIPT.read_text().splitlines():
        if line.startswith(begin):
            inside = True
            continue
        if line.startswith(end):
            inside = False
            continue
        if inside:
            body.append(line)
    assert body, topic
    return "\n".join(body)


VERSION_SH = _extract("version resolution")
FORK_SH = _extract("fork resolution")


def _bash(version, fork):
    script = (
        'BOX_IMAGE_REGISTRY=ghcr.io/lagerdata/lager-box\n'
        'BOX_IMAGE_TAG_REF=""\nBOX_IMAGE_PULL=1\n'
        f'GIT_VERSION={shlex.quote(version)}\nFORK={shlex.quote(fork)}\n'
        + VERSION_SH + "\n" + FORK_SH + "\n"
        + 'printf "%s\\n" "$GIT_REF" "$GIT_CHECKOUT" "$BOX_IMAGE_TAG_REF" '
          '"$BOX_IMAGE_PULL" "$FORK_FETCH_CMD"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


@pytest.mark.parametrize("version", [
    "main", "de/my-branch", "v0.61.0", "0.61.0", "v1.2.3-rc1", SHA, SHA.upper(),
])
def test_bash_and_python_fork_resolvers_agree(version):
    proc = _bash(version, "octocat/lager")
    assert proc.returncode == 0, proc.stderr
    git_ref, checkout, image_ref, pull, fetch_cmd = proc.stdout.split("\n")[:5]
    _, py_ref, py_fetch = update_mod.resolve_fork_version_ref(version)
    assert git_ref == py_ref
    assert checkout == f"--detach {py_ref}"
    assert image_ref == "" and pull == "0", "a fork never pulls the GHCR image"
    assert fetch_cmd == (
        "GIT_TERMINAL_PROMPT=0 git fetch --force --no-tags "
        f"https://github.com/octocat/lager.git {py_fetch} && "
    )


@pytest.mark.parametrize("version", ["main", "v0.61.0", SHA])
def test_bash_without_fork_is_unchanged(version):
    proc = _bash(version, "")
    assert proc.returncode == 0, proc.stderr
    git_ref, checkout, _, pull, fetch_cmd = proc.stdout.split("\n")[:5]
    _, py_reset, _ = update_mod.resolve_version_ref(version)
    assert git_ref == py_reset
    assert fetch_cmd == "" and pull == "1"
    assert checkout == update_mod.resolve_version_ref(version)[0]


@pytest.mark.parametrize("fork,version", [
    ("x/y;id", "main"), ("https://github.com/x/y", "main"), ("x/..", "main"),
    ("octocat/lager", "main;id"), ("octocat/lager", "a b"), ("octocat/lager", "a..b"),
])
def test_bash_refuses_bad_input(fork, version):
    assert _bash(version, fork).returncode != 0


def test_install_forwards_only_flags_the_script_parses():
    text = DEPLOY_SCRIPT.read_text()
    assert re.search(r"^\s+--fork\)\s*$", text, re.M)
