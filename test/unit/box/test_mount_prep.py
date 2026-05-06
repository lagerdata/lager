# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for cli/commands/box/_mount_prep.py.

Drives every branch by mocking the SSH runner. No real box, no subprocess,
no network. Mirrors the importlib-by-path style from test_box_config.py so
the test stays decoupled from the rest of the package's import graph.
"""

import importlib.util
import os
import sys
import unittest

_MOUNT_PREP_PY = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..', '..', '..', 'cli', 'commands', 'box', '_mount_prep.py',
    )
)


def _load_mount_prep_module():
    name = "mount_prep_under_test"
    spec = importlib.util.spec_from_file_location(name, _MOUNT_PREP_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mp = _load_mount_prep_module()


class FakeSsh:
    """Records (box_ip, cmd) calls and replays canned (rc, stdout, stderr) responses.

    Tests pre-load `responses` as a list of triples popped in order. Any extra
    call past the end raises AssertionError so missing expectations show up
    immediately rather than silently returning empty data.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, box_ip, cmd):
        self.calls.append((box_ip, cmd))
        if not self.responses:
            raise AssertionError(f"unexpected ssh call after responses exhausted: {cmd!r}")
        return self.responses.pop(0)


def _stat_missing():
    return (1, "", "stat: cannot stat ...: No such file or directory\n")


def _stat_owner(owner):
    return (0, f"{owner}\n", "")


def _ok():
    return (0, "", "")


def _sudo_failed():
    return (1, "", "sudo: a password is required\n")


class HostPathMissing(unittest.TestCase):
    def test_creates_and_chowns_for_rw(self):
        ssh = FakeSsh([
            _stat_missing(),
            _ok(),  # mkdir + chown chained
        ])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "created")
        self.assertEqual(r.current_owner, "33:33")
        self.assertIn("mkdir -p", ssh.calls[1][1])
        self.assertIn("chown 33:33", ssh.calls[1][1])

    def test_readonly_skips_chown(self):
        ssh = FakeSsh([
            _stat_missing(),
            _ok(),  # mkdir only, no chown
        ])
        r = mp.ensure_host_path_owned(
            "1.2.3.4", "/RoData", readonly=True, ssh_runner=ssh,
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "created")
        self.assertIn("mkdir -p", ssh.calls[1][1])
        self.assertNotIn("chown", ssh.calls[1][1])


class HostPathExists(unittest.TestCase):
    def test_already_correct_owner_no_writes(self):
        ssh = FakeSsh([_stat_owner("33:33")])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "ok")
        self.assertEqual(len(ssh.calls), 1)

    def test_readonly_existing_skips_chown(self):
        ssh = FakeSsh([_stat_owner("1000:1000")])
        r = mp.ensure_host_path_owned(
            "1.2.3.4", "/RoData", readonly=True, ssh_runner=ssh,
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "ok_readonly")
        # Owner mismatch is fine for RO; we don't even check populated state.
        self.assertEqual(len(ssh.calls), 1)


class HostPathWrongOwner(unittest.TestCase):
    def test_empty_dir_top_level_chown(self):
        ssh = FakeSsh([
            _stat_owner("1000:1000"),
            (0, "", ""),  # find returns no entries -> empty
            _ok(),  # chown (no -R)
        ])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "chowned")
        self.assertFalse(r.is_populated)
        chown_cmd = ssh.calls[2][1]
        self.assertIn("chown 33:33", chown_cmd)
        self.assertNotIn("-R", chown_cmd)

    def test_populated_without_recursive_refuses(self):
        ssh = FakeSsh([
            _stat_owner("1000:1000"),
            (0, "/Hyphen/sub\n", ""),  # find found something
        ])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "refused_populated")
        self.assertTrue(r.is_populated)
        self.assertEqual(r.current_owner, "1000:1000")
        self.assertIn("--recursive-chown", r.message)
        self.assertIn("1000:1000", r.message)
        self.assertIn("-R", r.manual_fix)

    def test_populated_with_recursive_chowns(self):
        ssh = FakeSsh([
            _stat_owner("1000:1000"),
            (0, "/Hyphen/sub\n", ""),
            _ok(),  # chown -R
        ])
        r = mp.ensure_host_path_owned(
            "1.2.3.4", "/Hyphen", recursive=True, ssh_runner=ssh,
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "recursive_chowned")
        chown_cmd = ssh.calls[2][1]
        self.assertIn("chown -R 33:33", chown_cmd)


class SudoFailures(unittest.TestCase):
    def test_mkdir_sudo_failure(self):
        ssh = FakeSsh([_stat_missing(), _sudo_failed()])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "sudo_failed")
        self.assertIsNotNone(r.manual_fix)
        self.assertIn("sudo mkdir -p", r.manual_fix)
        self.assertIn("sudo chown 33:33", r.manual_fix)

    def test_chown_sudo_failure(self):
        ssh = FakeSsh([
            _stat_owner("1000:1000"),
            (0, "", ""),  # empty dir
            _sudo_failed(),
        ])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "sudo_failed")
        self.assertEqual(r.current_owner, "1000:1000")


class ManualFixCommand(unittest.TestCase):
    def test_quotes_paths_with_spaces(self):
        cmd = mp.manual_fix_command("/path with space")
        self.assertIn("'/path with space'", cmd)
        self.assertIn("33:33", cmd)
        self.assertNotIn(" -R ", cmd)

    def test_recursive_emits_dash_R(self):
        cmd = mp.manual_fix_command("/Hyphen", recursive=True)
        self.assertIn("chown -R 33:33", cmd)


if __name__ == "__main__":
    unittest.main()
