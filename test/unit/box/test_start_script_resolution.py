# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Which start_box.sh a `lager box-config` bounce runs.

`apply`, `restart`, `repair` and the rollback all reach start_box.sh through
`_bounce_container_rc`. These tests run the command it sends for real, under
`sh`, against a fake home directory laid out the way a box can be:

- flat: only ~/box/start_box.sh (what `lager install` / `lager update` leave)
- nested: only ~/box/box/start_box.sh (a checkout that was never flattened)
- both: a flattened copy at ~/box that a later, non-flattening git update left
  behind, beside the current tracked tree under ~/box/box
- wrapper: the top-level copy replaced by a shim that execs the nested one

Each fake script records which copy ran. The deployed copies are written
WITHOUT the executable bit, as git checks them out, so a direct
`./start_box.sh` cannot run them and only a `bash` invocation passes.

The same harness covers the build skip: a fake `docker` reports an image only
when told to, and the recorded build hash is written with the hasher that
`lager install` and `lager update` use.
"""

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cli.commands.box import _start_script
from cli.commands.box import config as box_config_cli


def _fake_script(label):
    # Records which copy ran, from which directory, and the env the bounce set.
    return (
        "#!/bin/bash\n"
        f'echo "{label}|$(pwd)|${{LAGER_APPLY_NETWORK_SWITCH:-}}|$*|${{LAGER_SKIP_BUILD:-}}" '
        '>> "$HOME/ran"\n'
    )


class _BoxHome:
    """A throwaway $HOME with a ~/box tree and a PATH that has no real docker."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.box = os.path.join(self.home, "box")
        self.nested = os.path.join(self.box, "box")
        os.makedirs(os.path.join(self.box, "lager"))
        # Any docker call from the bounce must not reach the real daemon.
        self.bin = os.path.join(self.home, "bin")
        os.makedirs(self.bin)
        self.write(os.path.join(self.bin, "docker"),
                   '#!/bin/sh\n[ -f "$HOME/has_image" ]\n', mode=0o755)
        self.hash_file = os.path.join(self.home, "build-hash")

    def cleanup(self):
        self._tmp.cleanup()

    def write(self, path, body, mode=0o644):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, mode)

    def top(self, label="top", mode=0o644):
        self.write(os.path.join(self.box, "start_box.sh"), _fake_script(label), mode)

    def nested_tree(self, label="nested", with_lager=True):
        self.write(os.path.join(self.nested, "start_box.sh"), _fake_script(label))
        if with_lager:
            os.makedirs(os.path.join(self.nested, "lager"), exist_ok=True)

    def run(self, cmd, timeout=60):
        env = {
            "HOME": self.home,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "LAGER_BUILD_HASH_FILE": self.hash_file,
        }
        p = subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, check=False,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def ran(self):
        path = os.path.join(self.home, "ran")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [line.rstrip("\n").split("|") for line in f]


class BounceResolvesTheDeployedScript(unittest.TestCase):

    def setUp(self):
        self.home = _BoxHome()
        self.addCleanup(self.home.cleanup)

    def _bounce(self, **kwargs):
        def runner(_box, cmd, timeout=None, **_kw):
            return self.home.run(cmd)

        with patch.object(box_config_cli, "default_ssh_runner", side_effect=runner):
            rc = box_config_cli._bounce_container_rc(None, "test-box", **kwargs)
        return rc, self.home.ran()

    def _assert_ran_once(self, ran, label, directory):
        self.assertEqual(len(ran), 1, f"expected exactly one start_box.sh run, got {ran}")
        self.assertEqual(ran[0][0], label)
        self.assertEqual(os.path.realpath(ran[0][1]), os.path.realpath(directory))

    def test_flat_layout_runs_the_top_level_copy(self):
        self.home.top()
        rc, ran = self._bounce()
        self.assertEqual(rc, box_config_cli._BOUNCE_OK)
        self._assert_ran_once(ran, "top", self.home.box)

    def test_nested_only_runs_the_nested_copy(self):
        self.home.nested_tree()
        rc, ran = self._bounce()
        self.assertEqual(rc, box_config_cli._BOUNCE_OK)
        self._assert_ran_once(ran, "nested", self.home.nested)

    def test_both_present_the_nested_copy_wins(self):
        """The top-level copy is executable and older; it must not run."""
        self.home.top(label="stale-top", mode=0o755)
        self.home.nested_tree()
        rc, ran = self._bounce()
        self.assertEqual(rc, box_config_cli._BOUNCE_OK)
        self._assert_ran_once(ran, "nested", self.home.nested)

    def test_wrapper_at_top_level_still_runs_the_nested_copy_once(self):
        """A hand-placed shim at ~/box/start_box.sh that execs the nested copy.
        The bounce reaches the nested script directly, so it runs exactly once
        and the shim is never needed."""
        self.home.write(
            os.path.join(self.home.box, "start_box.sh"),
            '#!/bin/bash\necho "wrapper|$(pwd)||" >> "$HOME/ran"\n'
            'cd "$HOME/box/box" && exec bash ./start_box.sh "$@"\n',
            mode=0o755,
        )
        self.home.nested_tree()
        rc, ran = self._bounce()
        self.assertEqual(rc, box_config_cli._BOUNCE_OK)
        self._assert_ran_once(ran, "nested", self.home.nested)

    def test_nested_script_without_its_lager_tree_is_not_chosen(self):
        """A flatten that stopped partway can leave box/start_box.sh behind
        after box/lager already moved up. start_box.sh builds from its own
        directory's lager/, so that copy cannot run; the top-level one can."""
        self.home.top()
        self.home.nested_tree(with_lager=False)
        rc, ran = self._bounce()
        self.assertEqual(rc, box_config_cli._BOUNCE_OK)
        self._assert_ran_once(ran, "top", self.home.box)

    def test_network_switch_confirmation_reaches_the_resolved_script(self):
        self.home.top(label="stale-top", mode=0o755)
        self.home.nested_tree()
        rc, ran = self._bounce(confirm_network_switch=True)
        self.assertEqual(rc, box_config_cli._BOUNCE_OK)
        self._assert_ran_once(ran, "nested", self.home.nested)
        self.assertEqual(ran[0][2], "1")

    def test_a_missing_script_is_a_failed_bounce(self):
        rc, ran = self._bounce()
        self.assertEqual(rc, box_config_cli._BOUNCE_FAILED)
        self.assertEqual(ran, [])


class BounceSkipsTheBuildOnlyWhenTheImageIsCurrent(unittest.TestCase):
    """LAGER_SKIP_BUILD is set only when a `lager` image exists AND the tree
    the bounce runs hashes to the recorded build hash. Every other case must
    build, because a wrong skip starts new code on an old image."""

    def setUp(self):
        self.home = _BoxHome()
        self.addCleanup(self.home.cleanup)

    def _tree(self, root, body="v1"):
        self.home.write(os.path.join(root, "lager", "docker", "box.Dockerfile"),
                        f"FROM scratch # {body}\n")
        self.home.write(os.path.join(root, "lager", "nets", "net.py"), f"# {body}\n")

    def _record_hash_of(self, root_expr):
        """What `lager install` / `lager update` write, from the same hasher."""
        from cli.commands.utility.update import _build_hash_shell_cmd
        rc, out, err = self.home.run(_build_hash_shell_cmd(root_expr))
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.strip(), "the hasher measured nothing")
        with open(self.home.hash_file, "w") as f:
            f.write(out)

    def _image(self):
        open(os.path.join(self.home.home, "has_image"), "w").close()

    def _skip_value(self):
        def runner(_box, cmd, timeout=None, **_kw):
            return self.home.run(cmd)

        with patch.object(box_config_cli, "default_ssh_runner", side_effect=runner):
            rc = box_config_cli._bounce_container_rc(None, "test-box")
        self.assertEqual(rc, box_config_cli._BOUNCE_OK)
        ran = self.home.ran()
        self.assertEqual(len(ran), 1, ran)
        return ran[0][4]

    def test_current_image_on_a_flat_box_skips_the_build(self):
        self.home.top()
        self._tree(self.home.box)
        self._record_hash_of('"$HOME/box"')
        self._image()
        self.assertEqual(self._skip_value(), "1")

    def test_current_image_for_a_nested_tree_skips_the_build(self):
        """The nested tree is measured as itself, with the same relative
        paths, so it matches what an update of that tree records."""
        self.home.top(label="stale-top", mode=0o755)
        self._tree(self.home.box, body="old")
        self.home.nested_tree()
        self._tree(self.home.nested, body="new")
        self._record_hash_of('"$HOME/box/box"')
        self._image()
        self.assertEqual(self._skip_value(), "1")

    def test_no_image_builds(self):
        self.home.top()
        self._tree(self.home.box)
        self._record_hash_of('"$HOME/box"')
        self.assertEqual(self._skip_value(), "")

    def test_tree_changed_since_the_recorded_hash_builds(self):
        self.home.top()
        self._tree(self.home.box)
        self._record_hash_of('"$HOME/box"')
        self._tree(self.home.box, body="edited")
        self._image()
        self.assertEqual(self._skip_value(), "")

    def test_hash_recorded_for_the_stale_top_level_tree_builds(self):
        """The image matches the older top-level tree, but the bounce runs
        the newer nested one, so the image is not current for it."""
        self.home.top(label="stale-top", mode=0o755)
        self._tree(self.home.box, body="old")
        self.home.nested_tree()
        self._tree(self.home.nested, body="new")
        self._record_hash_of('"$HOME/box"')
        self._image()
        self.assertEqual(self._skip_value(), "")

    def test_no_recorded_hash_builds(self):
        self.home.top()
        self._tree(self.home.box)
        self._image()
        self.assertEqual(self._skip_value(), "")

    def test_an_empty_tree_never_matches_an_empty_record(self):
        self.home.top()
        open(self.home.hash_file, "w").close()
        self._image()
        self.assertEqual(self._skip_value(), "")

    def test_the_cli_says_when_it_reused_the_image(self):
        stdout = ("[1/1] Skipping build (LAGER_SKIP_BUILD set; image already "
                  "built by caller)\n")
        with patch.object(box_config_cli, "default_ssh_runner",
                          return_value=(0, stdout, "")), \
             patch.object(box_config_cli.click, "echo") as echo:
            box_config_cli._bounce_container_rc(None, "test-box")
        said = " ".join(str(c.args[0]) for c in echo.call_args_list if c.args)
        self.assertIn("reused it without rebuilding", said)

    def test_the_marker_matches_start_box_sh(self):
        """The CLI keys its message on start_box.sh's own line; if that line
        is reworded the message silently stops, so pin the two together."""
        here = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(here, "..", "..", "..", "box", "start_box.sh")
        with open(script) as f:
            self.assertIn(_start_script.SKIPPED_BUILD_MARKER, f.read())


class BounceCommandShape(unittest.TestCase):
    """The command text itself, for the properties a fake home cannot show."""

    def _command(self, **kwargs):
        with patch.object(box_config_cli, "default_ssh_runner",
                          return_value=(0, "", "")) as run:
            box_config_cli._bounce_container_rc(None, "test-box", **kwargs)
        return run.call_args.args[1]

    def test_invoked_with_bash_not_executed_directly(self):
        for kwargs in ({}, {"confirm_network_switch": True}):
            cmd = self._command(**kwargs)
            self.assertIn("bash ./start_box.sh", cmd)
            # Every mention of the script is an argument to bash.
            self.assertNotRegex(cmd, r"(?<!bash )\./start_box\.sh")

    def test_passes_the_skip_verdict_to_start_box_sh(self):
        cmd = self._command()
        self.assertIn('LAGER_SKIP_BUILD="$_lager_skip_build" bash ./start_box.sh', cmd)

    def test_uses_the_shared_resolver(self):
        self.assertIn(_start_script.RESOLVE_START_DIR, self._command())


class ResolverIsPosix(unittest.TestCase):
    """The remote login shell may be dash. `sh -n` rejects bash-only syntax
    only loosely, so run the snippet under sh and check what it picks."""

    def test_resolver_prints_the_chosen_directory(self):
        home = _BoxHome()
        self.addCleanup(home.cleanup)
        home.nested_tree()
        rc, out, err = home.run(
            _start_script.RESOLVE_START_DIR
            + f'; echo "${_start_script.START_DIR_VAR}"'
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(os.path.realpath(out.strip()), os.path.realpath(home.nested))


if __name__ == "__main__":
    unittest.main()
