# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/box_config/render_docker_args.py.

Verifies that the sourceable file the renderer writes is (a) syntactically
valid bash, and (b) preserves docker-run args verbatim through `source` +
array expansion. The previous stdout-based contract silently word-split
env values with whitespace; these tests pin that regression.

Test mechanics: invoke the renderer as a subprocess (avoids importlib
gymnastics for cfg), then `bash -c 'source out.sh; ...'` to inspect the
resulting arrays. No mocking — the renderer is pure I/O.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest


_RENDERER = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..', '..', '..', 'box', 'lager', 'box_config', 'render_docker_args.py',
    )
)


def _render(config_dict, *, write_file=True):
    """Run the renderer in a tempdir and return (rc, out_path, out_body, stderr).

    write_file=False writes no input config — exercises the FileNotFoundError
    branch.
    """
    d = tempfile.mkdtemp(prefix="lager-render-test-")
    try:
        config_path = os.path.join(d, "box_config.json")
        out_path = os.path.join(d, "box_config.docker.sh")
        if write_file:
            with open(config_path, "w") as f:
                json.dump(config_dict, f)
        proc = subprocess.run(
            ["python3", _RENDERER, config_path, out_path],
            capture_output=True,
            text=True,
        )
        body = ""
        if os.path.exists(out_path):
            with open(out_path) as f:
                body = f.read()
        return proc.returncode, out_path, body, proc.stderr
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _source_and_dump(body):
    """Source `body` in a bash subprocess and return the three arrays as
    Python lists. Uses NUL-separated `printf '%s\\0'` so element boundaries
    survive across the pipe regardless of whitespace inside elements."""
    script = (
        body
        + "printf 'MOUNTS_BEGIN\\0'\n"
        + 'for x in "${BOX_CONFIG_MOUNTS[@]}"; do printf "%s\\0" "$x"; done\n'
        + "printf 'ENV_BEGIN\\0'\n"
        + 'for x in "${BOX_CONFIG_ENV[@]}"; do printf "%s\\0" "$x"; done\n'
        + "printf 'PATHS_BEGIN\\0'\n"
        + 'for x in "${BOX_CONFIG_HOST_PATHS[@]}"; do printf "%s\\0" "$x"; done\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"bash failed: {proc.stderr!r}")
    parts = proc.stdout.split(b"\0")
    # parts ends with a trailing empty element from the last \0.
    mounts, env, paths = [], [], []
    bucket = None
    for p in parts:
        if p == b"MOUNTS_BEGIN":
            bucket = mounts
        elif p == b"ENV_BEGIN":
            bucket = env
        elif p == b"PATHS_BEGIN":
            bucket = paths
        elif p == b"":
            continue
        elif bucket is not None:
            bucket.append(p.decode())
    return mounts, env, paths


class BasicRendering(unittest.TestCase):
    def test_empty_config_renders_empty_arrays(self):
        rc, _, body, _ = _render({"version": 1})
        self.assertEqual(rc, 0)
        self.assertIn("BOX_CONFIG_MOUNTS=()", body)
        self.assertIn("BOX_CONFIG_ENV=()", body)
        self.assertIn("BOX_CONFIG_HOST_PATHS=()", body)
        mounts, env, paths = _source_and_dump(body)
        self.assertEqual(mounts, [])
        self.assertEqual(env, [])
        self.assertEqual(paths, [])

    def test_simple_mount_round_trips(self):
        rc, _, body, _ = _render({
            "version": 1,
            "mounts": [{"host": "/a", "container": "/b", "readonly": False}],
        })
        self.assertEqual(rc, 0)
        mounts, _, paths = _source_and_dump(body)
        self.assertEqual(mounts, ["-v", "/a:/b"])
        self.assertEqual(paths, ["/a"])

    def test_readonly_mount_gets_ro_suffix(self):
        rc, _, body, _ = _render({
            "version": 1,
            "mounts": [{"host": "/a", "container": "/b", "readonly": True}],
        })
        self.assertEqual(rc, 0)
        mounts, _, _ = _source_and_dump(body)
        self.assertEqual(mounts, ["-v", "/a:/b:ro"])

    def test_volume_uses_v_flag(self):
        rc, _, body, _ = _render({
            "version": 1,
            "volumes": [{"name": "box-tools", "container": "/opt/box-tools"}],
        })
        self.assertEqual(rc, 0)
        mounts, _, paths = _source_and_dump(body)
        self.assertEqual(mounts, ["-v", "box-tools:/opt/box-tools"])
        # Volumes are not bind-mount host paths, so paths stays empty.
        self.assertEqual(paths, [])

    def test_env_round_trips(self):
        rc, _, body, _ = _render({
            "version": 1,
            "env": {"FOO": "1", "BAR": "two"},
        })
        self.assertEqual(rc, 0)
        _, env, _ = _source_and_dump(body)
        # Order is dict-iteration order; check membership.
        self.assertIn("--env", env)
        self.assertIn("FOO=1", env)
        self.assertIn("BAR=two", env)


class QuotingSafety(unittest.TestCase):
    """The whole reason for the bash-array format: env values and paths can
    contain whitespace, $, backticks, single-quotes. None of those should
    leak through bash parsing into separate args or substitutions."""

    def test_env_value_with_whitespace_stays_one_element(self):
        rc, _, body, _ = _render({
            "version": 1,
            "env": {"GREETING": "hello world"},
        })
        self.assertEqual(rc, 0)
        _, env, _ = _source_and_dump(body)
        self.assertEqual(env, ["--env", "GREETING=hello world"])

    def test_env_value_with_dollar_sign_is_literal(self):
        rc, _, body, _ = _render({
            "version": 1,
            "env": {"PRICE": "$5.00"},
        })
        self.assertEqual(rc, 0)
        _, env, _ = _source_and_dump(body)
        self.assertEqual(env, ["--env", "PRICE=$5.00"])

    def test_env_value_with_backtick_is_not_executed(self):
        # If quoting were wrong, `id` would be executed and the value would
        # become whatever `id` printed. shlex.quote uses single quotes which
        # disable command substitution entirely.
        rc, _, body, _ = _render({
            "version": 1,
            "env": {"INJECT": "before `id` after"},
        })
        self.assertEqual(rc, 0)
        _, env, _ = _source_and_dump(body)
        self.assertEqual(env, ["--env", "INJECT=before `id` after"])

    def test_env_value_with_single_quote(self):
        rc, _, body, _ = _render({
            "version": 1,
            "env": {"GREETING": "it's fine"},
        })
        self.assertEqual(rc, 0)
        _, env, _ = _source_and_dump(body)
        self.assertEqual(env, ["--env", "GREETING=it's fine"])

    def test_mount_path_with_space_stays_one_element(self):
        rc, _, body, _ = _render({
            "version": 1,
            "mounts": [{"host": "/path with space", "container": "/c", "readonly": False}],
        })
        self.assertEqual(rc, 0)
        mounts, _, paths = _source_and_dump(body)
        # spec is "/path with space:/c" — one element after the -v flag.
        self.assertEqual(mounts, ["-v", "/path with space:/c"])
        self.assertEqual(paths, ["/path with space"])


class UdevRulesIgnored(unittest.TestCase):
    """Regression guard for the 0.23.0 udev_rules field: it's host-side only
    and must never leak into docker run args. Rendering a config with udev
    rules must produce byte-identical output to the same config without them."""

    def test_udev_rules_do_not_change_docker_args(self):
        base = {
            "version": 1,
            "mounts": [{"host": "/a", "container": "/b", "readonly": False}],
            "volumes": [{"name": "box-tools", "container": "/opt/box-tools"}],
            "env": {"FOO": "1"},
        }
        with_udev = dict(base, udev_rules=[
            {"vid": "1209", "pid": "0001", "mode": "0666", "usbtmc": False},
            {"vid": "1ab1", "pid": "0e11", "mode": "0660", "usbtmc": True},
        ])
        rc_a, _, body_a, _ = _render(base)
        rc_b, _, body_b, _ = _render(with_udev)
        self.assertEqual(rc_a, 0)
        self.assertEqual(rc_b, 0)
        self.assertEqual(body_a, body_b)
        # And nothing udev-shaped leaked into the rendered bash.
        self.assertNotIn("1209", body_b)
        self.assertNotIn("udev", body_b.lower())


class SoftFailBehavior(unittest.TestCase):
    """Validation/parse failures must still write a file with empty arrays
    so start_box.sh sources a known-good no-op rather than holding stale
    arrays from a previous run."""

    def test_missing_config_writes_empty_file_rc0(self):
        rc, _, body, _ = _render({}, write_file=False)
        self.assertEqual(rc, 0)
        self.assertIn("BOX_CONFIG_MOUNTS=()", body)

    def test_invalid_json_writes_empty_file_rc1(self):
        d = tempfile.mkdtemp(prefix="lager-render-test-")
        try:
            config_path = os.path.join(d, "box_config.json")
            out_path = os.path.join(d, "out.sh")
            with open(config_path, "w") as f:
                f.write("{ not json")
            proc = subprocess.run(
                ["python3", _RENDERER, config_path, out_path],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("invalid JSON", proc.stderr)
            self.assertTrue(os.path.exists(out_path))
            with open(out_path) as f:
                body = f.read()
            self.assertIn("BOX_CONFIG_MOUNTS=()", body)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_validation_failure_writes_empty_file_rc1(self):
        # migrate_raw rejects version > SCHEMA_VERSION ahead of validate, so
        # the error message comes from there. Either path counts — what
        # matters is that the renderer fails closed with empty arrays.
        rc, _, body, stderr = _render({"version": 999})
        self.assertEqual(rc, 1)
        self.assertTrue(
            "newer than this CLI supports" in stderr or "Unsupported config version" in stderr,
            f"unexpected stderr: {stderr!r}",
        )
        self.assertIn("BOX_CONFIG_MOUNTS=()", body)


if __name__ == "__main__":
    unittest.main()
