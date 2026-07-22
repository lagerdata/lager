# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the pip / cargo / npm box_config renderers:
  box/lager/box_config/render_pip_requirements.py   -> user_requirements.txt
  box/lager/box_config/render_cargo_packages.py     -> cargo_packages.txt
  box/lager/box_config/render_npm_packages.py       -> npm_packages.txt

These three had no direct coverage before. All share one interface:
`python3 render_X.py <box_config.json> <out_path>` writes a header plus one
sorted spec per line and soft-fails (empty/header-only file) on missing config.

The 0.23.0 regression of interest: each renderer reads ONLY its own field, so
adding the host-side `udev_rules` field to the schema must not change any of
their output. Each renderer is exercised as a subprocess (no importlib games).
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest


_RENDER_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..', '..', '..', 'box', 'lager', 'box_config',
    )
)

# (renderer filename, config field it reads, sample specs)
_RENDERERS = [
    ("render_pip_requirements.py", "pip_packages", ["requests==2.28.2", "rich", "httpx"]),
    ("render_cargo_packages.py", "cargo_packages", ["ripgrep@14.0.0", "defmt-print"]),
    ("render_npm_packages.py", "npm_packages", ["@angular/core@17.0.0", "express"]),
]


def _render(renderer, config_dict, *, write_file=True):
    """Run a package renderer in a tempdir; return (rc, body, stderr)."""
    d = tempfile.mkdtemp(prefix="lager-render-pkg-")
    try:
        config_path = os.path.join(d, "box_config.json")
        out_path = os.path.join(d, "out.txt")
        if write_file:
            with open(config_path, "w") as f:
                json.dump(config_dict, f)
        proc = subprocess.run(
            ["python3", os.path.join(_RENDER_DIR, renderer), config_path, out_path],
            capture_output=True,
            text=True,
        )
        body = ""
        if os.path.exists(out_path):
            with open(out_path) as f:
                body = f.read()
        return proc.returncode, body, proc.stderr
    finally:
        shutil.rmtree(d, ignore_errors=True)


class RendersOwnField(unittest.TestCase):
    def test_specs_rendered_sorted(self):
        for renderer, field, specs in _RENDERERS:
            with self.subTest(renderer=renderer):
                rc, body, _ = _render(renderer, {"version": 1, field: specs})
                self.assertEqual(rc, 0)
                lines = [ln for ln in body.splitlines() if ln and not ln.startswith("#")]
                self.assertEqual(lines, sorted(specs))

    def test_empty_field_is_header_only(self):
        for renderer, field, _ in _RENDERERS:
            with self.subTest(renderer=renderer):
                rc, body, _ = _render(renderer, {"version": 1})
                self.assertEqual(rc, 0)
                lines = [ln for ln in body.splitlines() if ln and not ln.startswith("#")]
                self.assertEqual(lines, [])

    def test_missing_config_soft_fails_rc0(self):
        for renderer, _, _ in _RENDERERS:
            with self.subTest(renderer=renderer):
                rc, body, _ = _render(renderer, {}, write_file=False)
                self.assertEqual(rc, 0)
                self.assertIn("rendered from", body)  # header still written


class UdevRulesIgnored(unittest.TestCase):
    """Regression guard for the 0.23.0 udev_rules field: it is host-side only
    and must not influence any package-requirements file."""

    def test_udev_rules_do_not_change_output(self):
        for renderer, field, specs in _RENDERERS:
            with self.subTest(renderer=renderer):
                base = {"version": 1, field: specs}
                with_udev = dict(base, udev_rules=[
                    {"vid": "1209", "pid": "0001", "mode": "0666", "usbtmc": False},
                    {"vid": "1ab1", "pid": "0e11", "mode": "0660", "usbtmc": True},
                ])
                rc_a, body_a, _ = _render(renderer, base)
                rc_b, body_b, _ = _render(renderer, with_udev)
                self.assertEqual(rc_a, 0)
                self.assertEqual(rc_b, 0)
                self.assertEqual(body_a, body_b)
                self.assertNotIn("1209", body_b)


class UnwritableOutputDir(unittest.TestCase):
    """A renderer that cannot write its output must say so, loudly.

    This is the failure that made `lager box-config` a silent no-op fleet-wide:
    /etc/lager is owned by the container user (uid 33), start_box.sh runs as the
    box's login user, and creating a file needs write permission on the
    DIRECTORY. Every renderer died with a bare PermissionError traceback that
    start_box.sh folded into a one-line warning, so `apply` reported success
    while installing nothing. Each renderer must now exit non-zero with an
    actionable message naming the directory.
    """

    def _render_into_readonly_dir(self, renderer, config_dict):
        d = tempfile.mkdtemp(prefix="lager-render-ro-")
        ro_dir = os.path.join(d, "etc-lager")
        os.mkdir(ro_dir)
        try:
            config_path = os.path.join(d, "box_config.json")
            with open(config_path, "w") as f:
                json.dump(config_dict, f)
            out_path = os.path.join(ro_dir, "out.txt")
            os.chmod(ro_dir, 0o555)  # r-x: cannot create the tmp file
            proc = subprocess.run(
                ["python3", os.path.join(_RENDER_DIR, renderer), config_path, out_path],
                capture_output=True,
                text=True,
            )
            return proc.returncode, proc.stdout, proc.stderr, out_path
        finally:
            os.chmod(ro_dir, 0o755)
            shutil.rmtree(d, ignore_errors=True)

    def test_every_renderer_fails_loudly_on_unwritable_dir(self):
        for renderer, field, specs in _RENDERERS + [
            ("render_docker_args.py", "pip_packages", []),
        ]:
            with self.subTest(renderer=renderer):
                rc, stdout, stderr, out_path = self._render_into_readonly_dir(
                    renderer, {"version": 1, field: specs}
                )
                out = stdout + stderr
                self.assertNotEqual(rc, 0, "must not report success")
                self.assertIn("[ERROR]", out)
                self.assertIn("cannot write", out)
                # Actionable: names the directory at fault and the repair.
                self.assertIn(os.path.dirname(out_path), out)
                self.assertIn("lager update", out)
                # A traceback is what we are replacing; it must not resurface.
                self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()
