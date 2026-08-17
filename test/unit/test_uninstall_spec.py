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
import os
import tempfile
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
    """lager_key_matcher reads ~/.ssh/lager_box.pub through expanduser, so a
    temp HOME with (or without) a real pubkey file drives both branches —
    no patching of os.path, which is the process-global posixpath module."""

    def _cmd(self, pub=None):
        with tempfile.TemporaryDirectory() as home:
            if pub is not None:
                os.makedirs(os.path.join(home, ".ssh"))
                with open(os.path.join(home, ".ssh", "lager_box.pub"),
                          "w", encoding="utf-8") as fh:
                    fh.write(pub)
            # USERPROFILE is what expanduser reads on Windows.
            with mock.patch.dict(os.environ,
                                 {"HOME": home, "USERPROFILE": home}):
                return u.authorized_keys_cleanup_cmd()

    def test_uses_local_pubkey_blob_when_available(self):
        cmd = self._cmd(pub="ssh-ed25519 AAAATESTBLOB some-comment\n")
        # Match by the key blob, not the comment — comments vary across
        # installs; the blob is exact.
        self.assertIn("grep -vF 'AAAATESTBLOB'", cmd)
        self.assertNotIn("some-comment", cmd)
        self.assertIn("chmod 600", cmd)

    def test_falls_back_to_default_comment(self):
        self.assertIn(u._LAGER_KEY_COMMENT, self._cmd())

    def test_empty_result_is_tolerated(self):
        # grep exits 1 when every line matches (authorized_keys held only the
        # lager key); the pipeline must still complete so the file is emptied
        # rather than left untouched.
        self.assertIn("|| true", self._cmd())


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
            if remote.startswith("cat ") and u._PRIV_RESULTS_PATH in remote:
                # The privileged session writes name=OK per step and the
                # command reads it back over this channel.
                return mock.Mock(
                    returncode=0, stdout="lock_state=OK\n", stderr="",
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
            # --keep-config narrows the privileged session to the lock-state
            # step; the container teardown under test runs either way.
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

    def test_keep_config_clears_the_lock_state_it_dissolved(self):
        """The other half of dissolving: nobody released the lock, so the
        file still says locked:true. --keep-config is the one path where
        that file survives the uninstall.
        """
        _result, events, _released, _heartbeat = self._drive_uninstall()
        remote = " ".join(payload for kind, payload in events if kind == "ssh")
        self.assertIn("/etc/lager/lock.json", remote)
        self.assertIn("/etc/lager/lock.json.flock", remote)
        # Only the lock state -- the saved nets are the whole point of the flag.
        self.assertNotIn("rm -rf /etc/lager", remote)


class KeepConfigLockState(unittest.TestCase):
    """A lock whose ttl_seconds is null is never reaped -- the box's
    _is_expired() returns False outright on a null TTL -- so a locked:true
    left in a preserved /etc/lager is permanent: reinstall, and the box comes
    up held by a holder that no longer exists.
    """

    def test_step_targets_only_the_lock_files(self):
        _name, _desc, cmd = u.LOCK_STATE_PRIV_STEP
        self.assertIn("/etc/lager/lock.json", cmd)
        self.assertIn("/etc/lager/lock.json.flock", cmd)
        for keeper in ("saved_nets.json", "/etc/lager ", "-rf"):
            self.assertNotIn(keeper, cmd, keeper)

    def test_step_tolerates_absent_files(self):
        # /etc/lager may not exist at all (already uninstalled, or a box that
        # never had one). `rm -f` is what makes that a success rather than a
        # FAILED line in the summary.
        _name, _desc, cmd = u.LOCK_STATE_PRIV_STEP
        self.assertIn("rm -f", cmd)

    def test_step_does_not_mask_failure(self):
        # Same rule the other privileged steps follow: `|| true` would defeat
        # the per-step OK/FAIL reporting.
        self.assertNotIn("|| true", u.LOCK_STATE_PRIV_STEP[2])

    def test_it_is_the_keep_config_counterpart_of_etc_lager(self):
        # The two are mutually exclusive by construction: one removes the
        # directory, the other the lock state inside a directory being kept.
        self.assertNotEqual(u.LOCK_STATE_PRIV_STEP[0], u.ETC_LAGER_PRIV_STEP[0])
        names = [n for n, _d, _c in u.UNINSTALL_ALL_PRIV_STEPS]
        self.assertNotIn("lock_state", names, "governed by --keep-config, not --all")


if __name__ == "__main__":
    unittest.main()


class DryRunQueries(unittest.TestCase):
    """`--dry-run` inventories the box over SSH. Its query helper used to
    capture both streams with a 30s timeout and no allowance for a password
    prompt, so on a box without key auth every query stalled until the
    timeout, landed in a blanket `except Exception`, and returned None --
    which the inventory renders exactly like "the box does not have this".
    A confident, clean, entirely wrong report.
    """

    CONNECTIVITY_PROBE = "echo ok"

    def _dry_run(self, *, keys_work=True, query_times_out=False):
        """Returns (result, calls) where calls is [(argv, kwargs), ...]."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            remote = cmd[-1] if isinstance(cmd, (list, tuple)) else cmd
            if remote == self.CONNECTIVITY_PROBE:
                if keys_work or "NumberOfPasswordPrompts=1" in cmd:
                    return mock.Mock(returncode=0, stdout="ok", stderr="")
                return mock.Mock(
                    returncode=255, stdout="", stderr="Permission denied (publickey).",
                )
            if query_times_out:
                raise u.subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))
            return mock.Mock(returncode=0, stdout="", stderr="")

        pool = mock.Mock()
        pool.ensure_connection.return_value = False

        with mock.patch.object(u.subprocess, "run", fake_run), \
                mock.patch.object(u, "get_ssh_connection_pool", return_value=pool):
            result = CliRunner().invoke(
                u.uninstall, ["--ip", "10.0.0.1", "--yes", "--dry-run"],
            )
        return result, calls

    def _queries(self, calls):
        """The inventory queries, i.e. everything past the connectivity probe."""
        return [
            (argv, kwargs) for argv, kwargs in calls
            if argv[-1] != self.CONNECTIVITY_PROBE
        ]

    def test_key_auth_path_is_batch_mode_and_short(self):
        result, calls = self._dry_run(keys_work=True)
        self.assertEqual(result.exit_code, 0, result.output)
        queries = self._queries(calls)
        self.assertTrue(queries, "sanity: the dry run issued queries")
        for argv, kwargs in queries:
            self.assertIn("BatchMode=yes", argv)
            self.assertEqual(kwargs.get("timeout"), 30)

    def test_password_path_allows_time_for_a_human(self):
        result, calls = self._dry_run(keys_work=False)
        self.assertEqual(result.exit_code, 0, result.output)
        queries = self._queries(calls)
        self.assertTrue(queries, "sanity: the dry run issued queries")
        for argv, kwargs in queries:
            # BatchMode would refuse to prompt at all and report the whole
            # box as empty; 30s is shorter than finding and typing a password.
            self.assertNotIn("BatchMode=yes", argv)
            self.assertIn("NumberOfPasswordPrompts=1", argv)
            self.assertEqual(kwargs.get("timeout"), 120)

    def test_password_path_leaves_stderr_on_the_terminal(self):
        # ssh's prompt goes to /dev/tty but its diagnostics go to stderr;
        # capturing those while a human is being asked to type is how
        # "nothing happened for two minutes" gets produced.
        _result, calls = self._dry_run(keys_work=False)
        for _argv, kwargs in self._queries(calls):
            self.assertIsNone(kwargs.get("stderr"))
            self.assertIsNotNone(kwargs.get("stdout"), "stdout is still needed")

    def test_a_timed_out_query_is_not_reported_as_absence(self):
        result, _calls = self._dry_run(keys_work=True, query_times_out=True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("timed out", result.output)
        self.assertIn("not necessarily absent", result.output)


class LocalConfigCleanupReport(unittest.TestCase):
    """delete_box() writes the global ~/.lager only; every read merges it with
    the project .lager files found walking up from the cwd. A box defined in
    both is deleted AND still resolves, and the command said "Removed" — which
    sent people hunting for a bug in the box rather than in their config.
    """

    def _drive(self, *, deleted, survivors):
        def fake_run(cmd, **_kwargs):
            remote = cmd[-1] if isinstance(cmd, (list, tuple)) else cmd
            if remote.startswith("cat ") and u._PRIV_RESULTS_PATH in remote:
                return mock.Mock(returncode=0, stdout="lock_state=OK\n", stderr="")
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        pool = mock.Mock()
        pool.ensure_connection.return_value = False

        with mock.patch.object(u.subprocess, "run", fake_run), \
                mock.patch.object(u, "get_ssh_connection_pool", return_value=pool), \
                mock.patch.object(u, "get_box_name_by_ip", return_value="STG-2"), \
                mock.patch.object(u, "delete_box", return_value=deleted), \
                mock.patch.object(
                    u, "project_files_defining_box", return_value=survivors), \
                mock.patch.object(
                    bs, "acquire_box_lock", return_value=("acquired", {})), \
                mock.patch.object(bs, "release_box_lock", return_value=True), \
                mock.patch.object(bs, "get_lock_holder", return_value="test-holder"), \
                mock.patch.object(bs, "HeartbeatThread", return_value=mock.Mock()):
            return CliRunner().invoke(
                u.uninstall, ["--ip", "10.0.0.1", "--yes", "--keep-config"],
            )

    def test_names_the_project_file_that_still_defines_the_box(self):
        result = self._drive(deleted=True, survivors=["/work/proj/.lager"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("global ~/.lager", result.output)
        self.assertIn("still defined in", result.output)
        self.assertIn("/work/proj/.lager", result.output)

    def test_a_clean_delete_says_nothing_extra(self):
        result = self._drive(deleted=True, survivors=[])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Removed 'STG-2'", result.output)
        self.assertNotIn("still defined in", result.output)

    def test_a_project_only_box_is_not_reported_as_missing(self):
        # delete_box returns False because the global file never had it, but
        # the box is very much configured — "was not found" is the wrong
        # sentence for "found, but not somewhere this command may write".
        result = self._drive(deleted=False, survivors=["/work/proj/.lager"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("was not found", result.output)
        self.assertIn("still defined in", result.output)

    def test_genuinely_absent_still_says_so(self):
        result = self._drive(deleted=False, survivors=[])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("was not found", result.output)
