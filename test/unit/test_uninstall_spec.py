# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Pins `lager uninstall`'s privileged removal spec to the artifacts the modern
`lager install` / `lager box-config apply` actually create, so the two can't
silently drift apart again (the old --all glob missed 99-instrument.rules
across a year of releases, and every sudo step was masked by `|| true`).

Also covers the box-lock lifecycle across the teardown: removing the lager
container removes the lock server, so the lock session dissolves instead of
heartbeating and releasing into the void.
"""

import importlib
import unittest
from unittest import mock

from click.testing import CliRunner

u = importlib.import_module("cli.commands.utility.uninstall")
bs = importlib.import_module("cli.box_storage")


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


class LockDissolveOnContainerRemoval(unittest.TestCase):
    """Step 1 deletes the lager container, which is the process serving the
    :9000 lock API this command's own auto-lock lives in. Past that point
    heartbeats and the final release are POSTs to a server the command
    itself removed — guaranteed to fail, and the heartbeat warned about it
    on every successful uninstall. The session must dissolve instead.
    """

    LAGER_CONTAINER_REMOVAL = "docker rm -f lager"
    BOX_DIR_REMOVAL = "rm -rf ~/box"

    def _drive_uninstall(self, *, container_removal_ok=True):
        """Run the command end to end over a faked SSH transport.

        Returns (result, events, released, heartbeat). ``events`` interleaves
        every remote command with each heartbeat stop, so ordering — not just
        call counts — can be asserted.
        """
        events = []
        released = []
        heartbeat = mock.Mock()
        heartbeat.stop.side_effect = lambda: events.append(("heartbeat-stopped", ""))

        def fake_run(cmd, **_kwargs):
            remote = cmd[-1] if isinstance(cmd, (list, tuple)) else cmd
            events.append(("ssh", remote))
            if not container_removal_ok and self.LAGER_CONTAINER_REMOVAL in remote:
                return mock.Mock(
                    returncode=1, stdout="", stderr="Error: No such container: lager",
                )
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        # Force the un-multiplexed path: a real pool would open an SSH
        # master connection to 10.0.0.1.
        pool = mock.Mock()
        pool.ensure_connection.return_value = False

        with mock.patch.object(u.subprocess, "run", fake_run), \
                mock.patch.object(u, "get_ssh_connection_pool", return_value=pool), \
                mock.patch.object(u, "get_box_name_by_ip", return_value=None), \
                mock.patch.object(
                    bs, "acquire_box_lock", return_value=("acquired", {})), \
                mock.patch.object(
                    bs, "release_box_lock",
                    side_effect=lambda *a, **k: released.append(a) or True), \
                mock.patch.object(bs, "get_lock_holder", return_value="test-holder"), \
                mock.patch.object(bs, "HeartbeatThread", return_value=heartbeat):
            # --keep-config keeps the privileged sudo session out of this
            # test; the container teardown under test runs either way.
            result = CliRunner().invoke(
                u.uninstall, ["--ip", "10.0.0.1", "--yes", "--keep-config"],
            )
        return result, events, released, heartbeat

    def _index_of(self, events, needle):
        for i, (kind, payload) in enumerate(events):
            if needle in kind or needle in payload:
                return i
        self.fail(f"no event matching {needle!r} in {events}")
        return None  # unreachable; keeps linters quiet

    def test_successful_removal_dissolves_the_lock(self):
        result, events, released, heartbeat = self._drive_uninstall()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(heartbeat.start.called, "sanity: the heartbeat did run")
        self.assertEqual(
            released, [], "no release POST may be sent to the deleted server",
        )
        # Stopped as part of Step 1, not merely tidied up at exit: the four
        # remaining steps must run with no heartbeat behind them.
        self.assertLess(
            self._index_of(events, "heartbeat-stopped"),
            self._index_of(events, self.BOX_DIR_REMOVAL),
            "heartbeat must stop when the container goes, not at command exit",
        )

    def test_failed_removal_keeps_the_lock_session_alive(self):
        # The container may still be up (docker permissions, wedged daemon),
        # so a heartbeat failure is real signal again and the lock is real
        # enough to need releasing.
        result, events, released, _heartbeat = self._drive_uninstall(
            container_removal_ok=False,
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(released), 1, "an undissolved lock must be released")
        self.assertGreater(
            self._index_of(events, "heartbeat-stopped"),
            self._index_of(events, self.BOX_DIR_REMOVAL),
            "the heartbeat should survive to the end of the command",
        )

    def test_no_heartbeat_warning_on_a_successful_uninstall(self):
        # The user-visible symptom that started this.
        result, _events, _released, _heartbeat = self._drive_uninstall()
        self.assertNotIn("lock heartbeat", result.output)
        self.assertNotIn("relying on server TTL", result.output)


if __name__ == "__main__":
    unittest.main()
