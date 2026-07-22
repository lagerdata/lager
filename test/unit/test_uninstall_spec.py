# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Pins `lager uninstall`'s privileged removal spec to the artifacts the modern
`lager install` / `lager box-config apply` actually create, so the two can't
silently drift apart again (the old --all glob missed 99-instrument.rules
across a year of releases, and every sudo step was masked by `|| true`).
"""

import importlib
import unittest
from unittest import mock

u = importlib.import_module("cli.commands.utility.uninstall")


class PrivStepSpec(unittest.TestCase):
    def test_covers_modern_install_artifacts(self):
        joined = " ".join(cmd for _n, _d, cmd in u.UNINSTALL_ALL_PRIV_STEPS)
        for artifact in [
            "/etc/udev/rules.d/99-instrument.rules",
            "/etc/udev/rules.d/99-lager-user.rules",
            "/etc/modprobe.d/blacklist-usbtmc.conf",
            "/etc/sudoers.d/lagerdata-udev",
            "/etc/sudoers.d/lager-box-config",
            "/etc/sudoers.d/lager-bench-json",
            "/usr/local/lib/lager/secure_box_firewall.sh",
            "/etc/sysctl.d/99-lager-box-config.conf",
            "groupdel lager",
        ]:
            self.assertIn(artifact, joined, artifact)

    def test_udev_removal_reloads_rules(self):
        commands = {n: c for n, _d, c in u.UNINSTALL_ALL_PRIV_STEPS}
        self.assertIn("udevadm control --reload-rules", commands["udev_rules"])
        self.assertIn("udevadm trigger", commands["udev_rules"])

    def test_no_silent_failure_masking(self):
        # `|| true` inside a step would defeat the per-step OK/FAIL reporting
        # that replaced the old always-"done" behavior.
        for name, _desc, cmd in u.UNINSTALL_ALL_PRIV_STEPS + [u.ETC_LAGER_PRIV_STEP]:
            self.assertNotIn("|| true", cmd, name)

    def test_sudoers_removed_last(self):
        # Earlier steps may depend on the NOPASSWD grants (or on the sudo
        # timestamp cached by the session's first prompt).
        self.assertEqual(u.UNINSTALL_ALL_PRIV_STEPS[-1][0], "sudoers")

    def test_etc_lager_is_separate_from_all_steps(self):
        # /etc/lager is governed by --keep-config, not --all.
        names = [n for n, _d, _c in u.UNINSTALL_ALL_PRIV_STEPS]
        self.assertNotIn("etc_lager", names)
        self.assertEqual(u.ETC_LAGER_PRIV_STEP[0], "etc_lager")
        self.assertIn("rm -rf /etc/lager", u.ETC_LAGER_PRIV_STEP[2])

    def test_group_removal_is_rerun_safe(self):
        # A second uninstall (group already gone) must not report FAILED.
        commands = {n: c for n, _d, c in u.UNINSTALL_ALL_PRIV_STEPS}
        self.assertIn("getent group lager", commands["lager_group"])

    def test_ufw_reset_tolerates_missing_ufw(self):
        commands = {n: c for n, _d, c in u.UNINSTALL_ALL_PRIV_STEPS}
        self.assertIn("command -v ufw", commands["ufw_reset"])


class AuthorizedKeysCleanup(unittest.TestCase):
    def test_uses_local_pubkey_blob_when_available(self):
        with mock.patch.object(u.os.path, "isfile", return_value=True), \
                mock.patch("builtins.open", mock.mock_open(
                    read_data="ssh-ed25519 AAAATESTBLOB some-comment\n")):
            cmd = u.authorized_keys_cleanup_cmd()
        # Match by the key blob, not the comment — comments vary across
        # installs; the blob is exact.
        self.assertIn("grep -vF 'AAAATESTBLOB'", cmd)
        self.assertNotIn("some-comment", cmd)
        self.assertIn("chmod 600", cmd)

    def test_falls_back_to_default_comment(self):
        with mock.patch.object(u.os.path, "isfile", return_value=False):
            cmd = u.authorized_keys_cleanup_cmd()
        self.assertIn(u._LAGER_KEY_COMMENT, cmd)

    def test_empty_result_is_tolerated(self):
        # grep exits 1 when every line matches (authorized_keys held only the
        # lager key); the pipeline must still complete so the file is emptied
        # rather than left untouched.
        with mock.patch.object(u.os.path, "isfile", return_value=False):
            cmd = u.authorized_keys_cleanup_cmd()
        self.assertIn("|| true", cmd)


if __name__ == "__main__":
    unittest.main()
