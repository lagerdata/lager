# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for cli/commands/box/_mount_prep.py.

Drives every branch by mocking the SSH runner. No real box, no subprocess,
no network.
"""

import unittest

from cli.commands.box import _mount_prep as mp


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

    def test_sudo_failure_message_includes_bootstrap(self):
        # The whole point of the message change in PR2: a fresh box without
        # passwordless sudo should get a copy-pasteable sudoers snippet so
        # the user can fix it once and not be asked again.
        ssh = FakeSsh([_stat_missing(), _sudo_failed()])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertFalse(r.ok)
        self.assertIn("sudoers.d/lager-box-config", r.message)
        self.assertIn("NOPASSWD: /bin/mkdir, /bin/chown", r.message)

    def test_unexpected_sudo_error_skips_bootstrap(self):
        # If sudo failed for an unrelated reason (e.g. /bin/mkdir doesn't
        # exist), the bootstrap snippet wouldn't help and would just be
        # noise. Surface the raw stderr instead.
        ssh = FakeSsh([
            _stat_missing(),
            (1, "", "mkdir: cannot create directory: Read-only file system\n"),
        ])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertFalse(r.ok)
        self.assertNotIn("sudoers.d/lager-box-config", r.message)
        self.assertIn("Read-only file system", r.message)


def _ssh_dead():
    return (255, "", "boxuser@192.0.2.7: Permission denied (publickey,password).\r\n")


class SshTransportFailure(unittest.TestCase):
    def test_stat_ssh_failure_returns_ssh_failed(self):
        # rc 255 is ssh's own exit code; the path was never checked, so no
        # mkdir/chown attempt and no manual_fix suggesting one.
        ssh = FakeSsh([_ssh_dead()])
        r = mp.ensure_host_path_owned("192.0.2.7", "/usr/bin/dfu-util", ssh_runner=ssh)
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "ssh_failed")
        self.assertEqual(len(ssh.calls), 1)
        self.assertIsNone(r.manual_fix)

    def test_ssh_failure_message_names_fix(self):
        ssh = FakeSsh([_ssh_dead()])
        r = mp.ensure_host_path_owned(
            "192.0.2.7", "/usr/bin/dfu-util", ssh_runner=ssh, box_user="boxuser",
        )
        self.assertIn("ssh-copy-id boxuser@192.0.2.7", r.message)
        self.assertIn("--no-auto-prep", r.message)
        self.assertIn("Permission denied (publickey,password)", r.message)

    def test_ssh_failure_on_mkdir_call(self):
        # Transport dies BETWEEN stat and mkdir (flaky link, dropping VPN):
        # must classify as ssh_failed, not sudo_failed with raw ssh stderr.
        ssh = FakeSsh([_stat_missing(), _ssh_dead()])
        r = mp.ensure_host_path_owned("192.0.2.7", "/Hyphen", ssh_runner=ssh)
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "ssh_failed")
        self.assertIsNone(r.manual_fix)
        self.assertIn("ssh-copy-id", r.message)

    def test_ssh_failure_on_find_call(self):
        ssh = FakeSsh([_stat_owner("1000:1000"), _ssh_dead()])
        r = mp.ensure_host_path_owned("192.0.2.7", "/Hyphen", ssh_runner=ssh)
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "ssh_failed")
        self.assertIsNone(r.manual_fix)

    def test_ssh_failure_on_chown_call(self):
        ssh = FakeSsh([
            _stat_owner("1000:1000"),
            (0, "", ""),  # find: empty dir
            _ssh_dead(),
        ])
        r = mp.ensure_host_path_owned("192.0.2.7", "/Hyphen", ssh_runner=ssh)
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "ssh_failed")
        self.assertIsNone(r.manual_fix)

    def test_empty_stderr_renders_generic_detail(self):
        ssh = FakeSsh([(255, "", "")])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertEqual(r.action, "ssh_failed")
        self.assertIn("ssh exited 255", r.message)

    def test_banner_stderr_uses_last_line(self):
        # Login banners precede the actual error on stderr; the message
        # should show the denial, not the banner.
        ssh = FakeSsh([(255, "", "Authorized use only.\nPermission denied (publickey).\n")])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertIn("Permission denied (publickey)", r.message)
        self.assertNotIn("Authorized use only", r.message)

    def test_spaced_path_in_message(self):
        ssh = FakeSsh([_ssh_dead()])
        r = mp.ensure_host_path_owned("1.2.3.4", "/path with space", ssh_runner=ssh)
        self.assertEqual(r.action, "ssh_failed")
        self.assertIn("/path with space", r.message)

    def test_remote_stat_exit_1_still_means_missing(self):
        # Regression guard: a remote stat failing with rc 1 (path absent)
        # must keep taking the create branch, not be mistaken for ssh death.
        ssh = FakeSsh([_stat_missing(), _ok()])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "created")


class SudoersBootstrapUser(unittest.TestCase):
    def test_bootstrap_uses_resolved_user(self):
        ssh = FakeSsh([_stat_missing(), _sudo_failed()])
        r = mp.ensure_host_path_owned(
            "192.0.2.7", "/Hyphen", ssh_runner=ssh, box_user="boxuser",
        )
        self.assertFalse(r.ok)
        self.assertIn("boxuser ALL=(root)", r.message)
        self.assertNotIn("lagerdata", r.message)

    def test_bootstrap_defaults_to_lagerdata(self):
        ssh = FakeSsh([_stat_missing(), _sudo_failed()])
        r = mp.ensure_host_path_owned("1.2.3.4", "/Hyphen", ssh_runner=ssh)
        self.assertIn("lagerdata ALL=(root)", r.message)


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
