# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
cli/deployment/security/etc_lager_perms.sh -- the root-owned helper that
creates /etc/lager and sets its ownership.

`lager install` used to do this with `sudo find /etc/lager ... -exec chown` and
`sudo chown -R`. Neither can be granted: `find -exec` runs anything as root,
and a recursive chown cannot skip the one directory that must be skipped. So
both prompted for the sudo password on every install. The helper is a fixed
script, granted by exact path with no arguments.

The REAL script runs here, under `sh`, with the real `find`. Two of its lines
are rewritten to point it at a scratch directory, and `chown` and `id` are
fakes that record what they were asked: changing a file's owner needs
privilege, so the recorded command line is what a unit test can check.
"""
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest

REPO = pathlib.Path(__file__).resolve().parents[3]
HELPER = REPO / "cli" / "deployment" / "security" / "etc_lager_perms.sh"

SH = shutil.which("sh") or "/bin/sh"


class _HelperCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.etc_lager = root / "etc-lager"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.chown_log = root / "chown.log"

        self._fake("chown", f"""
            printf '%s\\n' "$@" >> {self.chown_log}
            echo '--' >> {self.chown_log}
        """)
        # `id -u` is how the script asks whether it is root; `id -g USER` is
        # its fallback when sudo set SUDO_USER but not SUDO_GID.
        self._fake("id", """
            case "$1" in
              -u) echo "${FAKE_UID:-0}" ;;
              -g) echo "${FAKE_ID_G:-}" ;;
            esac
        """)

        text = HELPER.read_text(encoding="utf-8")
        text, n_path = re.subn(
            r"^PATH=.*$", f"PATH={self.bin}:/usr/bin:/bin", text, flags=re.M)
        text, n_dir = re.subn(
            r"^ETC_LAGER=.*$", f"ETC_LAGER={self.etc_lager}", text, flags=re.M)
        # Exactly one of each, or the harness is not testing the shipped paths.
        self.assertEqual((n_path, n_dir), (1, 1))
        self.script = root / "helper-under-test.sh"
        self.script.write_text(text, encoding="utf-8")

    def _fake(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)

    def _run(self, *args, **env):
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        base.update(env)
        return subprocess.run(
            [SH, str(self.script), *args], env=base, capture_output=True, text=True)

    def _chown_calls(self):
        if not self.chown_log.exists():
            return []
        calls, current = [], []
        for line in self.chown_log.read_text().splitlines():
            if line == "--":
                calls.append(current)
                current = []
            else:
                current.append(line)
        return calls

    def _chowned_paths(self):
        return [p for call in self._chown_calls() for p in call[2:]]


class WhatItDoes(_HelperCase):
    def _populate(self):
        self.etc_lager.mkdir()
        (self.etc_lager / "saved_nets.json").write_text("[]")
        (self.etc_lager / "sub").mkdir()
        (self.etc_lager / "sub" / "deep.json").write_text("{}")
        keys = self.etc_lager / "authorized_keys.d"
        keys.mkdir()
        (keys / "operator.pub").write_text("ssh-ed25519 AAAA")
        (keys / "nested").mkdir()
        (keys / "nested" / "other.pub").write_text("ssh-ed25519 BBBB")

    def test_everything_but_the_key_directory_goes_to_uid_33_and_the_callers_group(self):
        self._populate()
        result = self._run(SUDO_GID="4321", SUDO_UID="1000", SUDO_USER="operator")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self._chown_calls()
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call[:2], ["-h", "33:4321"], call)
        chowned = set(self._chowned_paths())
        for expected in ("", "saved_nets.json", "sub", "sub/deep.json"):
            self.assertIn(str(self.etc_lager / expected).rstrip("/"), chowned)

    def test_the_key_directory_and_everything_in_it_is_left_alone(self):
        # Sweeping it into uid 33 lets code in the container authorize its
        # own SSH key. This is the reason a plain recursive chown is wrong.
        self._populate()
        self._run(SUDO_GID="4321", SUDO_UID="1000")
        touched = [p for p in self._chowned_paths() if "authorized_keys.d" in p]
        self.assertEqual(touched, [])

    def test_a_symbolic_link_is_passed_with_h_so_its_target_is_never_re_owned(self):
        self._populate()
        outside = pathlib.Path(self.tmp.name) / "outside-secret"
        outside.write_text("not ours")
        (self.etc_lager / "planted").symlink_to(outside)
        result = self._run(SUDO_GID="4321", SUDO_UID="1000")
        self.assertEqual(result.returncode, 0, result.stderr)
        chowned = self._chowned_paths()
        self.assertIn(str(self.etc_lager / "planted"), chowned)
        self.assertNotIn(str(outside), chowned)
        for call in self._chown_calls():
            self.assertEqual(call[0], "-h")

    def test_it_creates_the_directory_on_a_fresh_box_setgid_and_group_writable(self):
        self.assertFalse(self.etc_lager.exists())
        result = self._run(SUDO_GID="4321", SUDO_UID="1000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.etc_lager.is_dir())
        self.assertEqual(stat.S_IMODE(self.etc_lager.stat().st_mode), 0o2775)
        self.assertIn(str(self.etc_lager), self._chowned_paths())

    def test_running_it_twice_is_harmless(self):
        self._populate()
        first = self._run(SUDO_GID="4321", SUDO_UID="1000")
        second = self._run(SUDO_GID="4321", SUDO_UID="1000")
        self.assertEqual((first.returncode, second.returncode), (0, 0))

    def test_the_group_falls_back_to_the_sudo_user_when_sudo_gid_is_absent(self):
        result = self._run(SUDO_USER="operator", SUDO_UID="1000", FAKE_ID_G="5555")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._chown_calls()[0][:2], ["-h", "33:5555"])

    def test_root_as_the_login_user_may_use_group_0(self):
        result = self._run(SUDO_GID="0", SUDO_UID="0", SUDO_USER="root")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._chown_calls()[0][:2], ["-h", "33:0"])


class WhatItRefuses(_HelperCase):
    def _assert_refused(self, result, code):
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(self._chown_calls(), [])
        self.assertFalse(self.etc_lager.exists())
        self.assertTrue(result.stderr.strip())

    def test_any_argument_at_all(self):
        # The sudoers grant names the bare path. An argument could only come
        # from a caller that is not the grant, so there is nothing to parse.
        for args in (["/etc"], ["--help"], [""], ["a", "b"]):
            self._assert_refused(self._run(*args, SUDO_GID="4321", SUDO_UID="1000"), 64)

    def test_a_caller_that_is_not_root(self):
        self._assert_refused(self._run(SUDO_GID="4321", SUDO_UID="1000", FAKE_UID="1000"), 77)

    def test_no_sudo_environment_at_all(self):
        self._assert_refused(self._run(), 78)

    def test_a_group_that_is_not_a_plain_number(self):
        for bad in ("1000;id", "-1", " ", "10 00", "abc", "$(id)", "1000\n0"):
            self._assert_refused(self._run(SUDO_GID=bad, SUDO_UID="1000"), 78)

    def test_group_0_for_a_login_user_that_is_not_root(self):
        self._assert_refused(self._run(SUDO_GID="0", SUDO_UID="1000"), 78)
        self._assert_refused(self._run(SUDO_GID="0"), 78)


class TheShippedText(unittest.TestCase):
    def setUp(self):
        self.text = HELPER.read_text(encoding="utf-8")
        self.code = "\n".join(
            ln for ln in self.text.splitlines() if not ln.lstrip().startswith("#"))

    def test_it_is_posix_sh_and_parses(self):
        self.assertTrue(self.text.startswith("#!/bin/sh\n"))
        result = subprocess.run([SH, "-n", str(HELPER)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_it_ships_the_real_paths(self):
        self.assertIn("\nETC_LAGER=/etc/lager\n", self.text)
        self.assertIn("\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\n", self.text)
        self.assertIn("CONTAINER_UID=33", self.code)

    def test_no_recursive_chown_and_no_dereference(self):
        self.assertNotRegex(self.code, r"chown\s+(-\w*R|--recursive)")
        self.assertRegex(self.code, r"chown -h ")
        self.assertIn("-prune", self.code)

    def test_it_never_reads_its_arguments(self):
        # `$#` is the only thing it may know about them.
        self.assertNotRegex(self.code, r'\$[1-9@*]|\$\{[1-9@*]')

    def test_it_is_packaged_with_the_cli(self):
        setup_py = (REPO / "cli" / "setup.py").read_text(encoding="utf-8")
        self.assertIn("'cli.deployment.security': ['*.sh']", setup_py)


if __name__ == "__main__":
    unittest.main()
