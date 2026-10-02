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

import fnmatch
import importlib
import os
import subprocess
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
            "/usr/local/lib/lager/etc_lager_perms.sh",
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
        purge_steps = [
            u.etc_lager_backup_step("~/b"),
            u.etc_lager_purge_step("~/b", include_control_plane=False),
            u.etc_lager_purge_step("~/b", include_control_plane=True),
        ]
        for name, _desc, cmd in u.UNINSTALL_ALL_PRIV_STEPS + purge_steps:
            self.assertNotIn("|| true", cmd, name)

    def test_sudoers_removed_last(self):
        # Earlier steps may depend on the NOPASSWD grants (or on the sudo
        # timestamp cached by the session's first prompt).
        self.assertEqual(u.UNINSTALL_ALL_PRIV_STEPS[-1][0], "sudoers")

    def test_etc_lager_is_separate_from_all_steps(self):
        # /etc/lager is governed by --purge-config, not --all.
        names = [n for n, _d, _c in u.UNINSTALL_ALL_PRIV_STEPS]
        self.assertNotIn("etc_lager", names)
        self.assertNotIn("config_backup", names)

    def test_no_purge_removes_the_directory_itself(self):
        # Every purge keeps the key registrations, so none may rm the
        # directory wholesale.
        for include_control_plane in (False, True):
            cmd = u.etc_lager_purge_step("~/b", include_control_plane)[2]
            self.assertNotIn("rm -rf /etc/lager", cmd)
            self.assertIn("! -name 'authorized_keys.d'", cmd)

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
    no patching of os.path, which is the process-global posixpath module.

    The command is also RUN, against a temp HOME and key directory, because
    what matters is which lines survive, not which words the command holds.
    A fleet sharing one lager_box key lost it for every operator when one of
    them ran `--all`: the old strip removed the blob anywhere in the file.
    """

    BLOB = "AAAATESTBLOB"
    KEY = f"ssh-ed25519 {BLOB} lager-box-access"
    OTHER = "ssh-ed25519 AAAAOTHERKEY someone@laptop"

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

    def _run(self, authorized_keys, registrations=()):
        """Run the cleanup on a box faked under a temp dir.

        Returns (status word, resulting authorized_keys text).
        """
        cmd = self._cmd(pub=f"{self.KEY}\n")
        with tempfile.TemporaryDirectory() as box_home, \
                tempfile.TemporaryDirectory() as keys_dir:
            os.makedirs(os.path.join(box_home, ".ssh"))
            ak = os.path.join(box_home, ".ssh", "authorized_keys")
            with open(ak, "w", encoding="utf-8") as fh:
                fh.write(authorized_keys)
            for i, line in enumerate(registrations):
                with open(os.path.join(keys_dir, f"r{i}.pub"), "w",
                          encoding="utf-8") as fh:
                    fh.write(line + "\n")
            cmd = cmd.replace(u.BOX_KEYS_DIR, keys_dir)
            out = subprocess.run(
                ["bash", "-c", cmd], capture_output=True, text=True,
                env={**os.environ, "HOME": box_home}, check=True,
            ).stdout.strip()
            with open(ak, encoding="utf-8") as fh:
                return out, fh.read()

    def _block(self, *lines):
        return "\n".join([u._AK_BEGIN, *lines, u._AK_END]) + "\n"

    def test_revokes_the_copy_in_lagers_block(self):
        status, text = self._run(self.OTHER + "\n" + self._block(self.KEY))
        self.assertEqual(status, "revoked")
        self.assertNotIn(self.BLOB, text)
        self.assertIn(self.OTHER, text)
        self.assertIn(u._AK_BEGIN, text, "the sentinels stay for the next sync")

    def test_a_loose_copy_is_left_in_place(self):
        # Not lager's to remove: ssh-copy-id, a person, or another manager put
        # it there, and other operators may share the key.
        status, text = self._run(self.KEY + "\n" + self._block(self.KEY))
        self.assertEqual(status, "revoked")
        self.assertEqual(text.count(self.BLOB), 1)
        self.assertTrue(text.startswith(self.KEY))

    def test_only_a_loose_copy_means_nothing_to_revoke(self):
        status, text = self._run(self.KEY + "\n" + self.OTHER + "\n")
        self.assertEqual(status, "not-found")
        self.assertIn(self.KEY, text)

    def test_another_manager_block_is_untouched(self):
        foreign = "\n".join([
            "# BEGIN OTHER MANAGED KEYS - do not edit by hand",
            self.KEY,
            "# END OTHER MANAGED KEYS",
        ]) + "\n"
        status, text = self._run(foreign + self._block(self.KEY))
        self.assertEqual(status, "revoked")
        self.assertEqual(text.count(self.BLOB), 1)
        self.assertIn("OTHER MANAGED KEYS", text)

    def test_kept_while_another_registration_holds_it(self):
        # Revoking would last only until the next key sync re-published it,
        # and the other registrant still relies on it.
        original = self._block(self.KEY)
        status, text = self._run(original, registrations=[self.KEY])
        self.assertEqual(status, "still-registered")
        self.assertEqual(text, original)

    def test_no_local_pubkey_changes_nothing(self):
        # The comment is shared by every lager_box key, so it is not a safe
        # matcher for a removal.
        self.assertEqual(self._cmd(), "echo no-local-key")

    def test_matches_by_blob_not_comment(self):
        cmd = self._cmd(pub="ssh-ed25519 AAAATESTBLOB some-comment\n")
        self.assertIn("AAAATESTBLOB", cmd)
        self.assertNotIn("some-comment", cmd)

    def test_loose_count_reports_copies_outside_the_block(self):
        cmd = None
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".ssh"))
            with open(os.path.join(home, ".ssh", "lager_box.pub"), "w",
                      encoding="utf-8") as fh:
                fh.write(self.KEY + "\n")
            with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
                cmd = u.loose_key_count_cmd()
            with open(os.path.join(home, ".ssh", "authorized_keys"), "w",
                      encoding="utf-8") as fh:
                fh.write(self.KEY + "\n" + self._block(self.KEY))
            out = subprocess.run(
                ["bash", "-c", cmd], capture_output=True, text=True,
                env={**os.environ, "HOME": home}, check=True,
            ).stdout.strip()
        self.assertEqual(out, "1")


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

    def test_it_is_the_default_counterpart_of_the_purge(self):
        # Mutually exclusive by construction: the purge removes the lock
        # files with everything else, the default only the lock state.
        self.assertNotEqual(
            u.LOCK_STATE_PRIV_STEP[0], u.etc_lager_purge_step("~/b", False)[0],
        )
        names = [n for n, _d, _c in u.UNINSTALL_ALL_PRIV_STEPS]
        self.assertNotIn("lock_state", names, "governed by --purge-config, not --all")


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


def _local(cmd, etc_lager):
    """A remote privileged command, made runnable here: /etc/lager points at
    a temp dir and sudo is dropped."""
    return cmd.replace("/etc/lager", etc_lager).replace("sudo ", "")


class PurgeKeepsWhatIsNotLagers(unittest.TestCase):
    """Deleting /etc/lager took a control-plane-managed box off its control
    plane (control_plane.json), put lager back onto the gateway's ports (the
    no_publish marker), and revoked keys other operators used (the
    authorized_keys.d registrations). The purge steps are run for real on a
    temp tree to pin exactly what survives.
    """

    LAGER_FILES = ("saved_nets.json", "box_config.json", "version", "ref", "lock.json")

    def _tree(self, root, *, control_plane):
        os.makedirs(os.path.join(root, "authorized_keys.d"))
        with open(os.path.join(root, "authorized_keys.d", "k.pub"), "w") as fh:
            fh.write("ssh-ed25519 AAAA k\n")
        names = list(self.LAGER_FILES) + ["no_publish"]
        if control_plane:
            names += ["control_plane.json", "telemetry_buffer.jsonl",
                      "org_secrets.json", "org_secrets.json.pre-manager"]
            os.makedirs(os.path.join(root, "dashboard"))
        for n in names:
            with open(os.path.join(root, n), "w") as fh:
                fh.write("[]")

    def _purge(self, *, control_plane, include_control_plane, backup_complete=True):
        with tempfile.TemporaryDirectory() as tmp:
            etc = os.path.join(tmp, "lager")
            backup = os.path.join(tmp, "backup")
            os.makedirs(backup)
            self._tree(etc, control_plane=control_plane)
            if backup_complete:
                open(os.path.join(backup, ".complete"), "w").close()
            _n, _d, cmd = u.etc_lager_purge_step(backup, include_control_plane)
            rc = subprocess.run(["bash", "-c", _local(cmd, etc)]).returncode
            return rc, sorted(os.listdir(etc))

    def test_plain_box_keeps_registrations_and_marker(self):
        rc, left = self._purge(control_plane=False, include_control_plane=False)
        self.assertEqual(rc, 0)
        self.assertEqual(left, ["authorized_keys.d", "no_publish"])

    def test_managed_box_keeps_control_plane_files(self):
        rc, left = self._purge(control_plane=True, include_control_plane=False)
        self.assertEqual(rc, 0)
        for pattern in u.CONTROL_PLANE_FILES + u.PURGE_ALWAYS_KEPT:
            self.assertTrue(fnmatch.filter(left, pattern), pattern)
        for name in self.LAGER_FILES:
            self.assertNotIn(name, left, "lager's own config is what a purge removes")

    def test_include_control_plane_keeps_only_registrations(self):
        rc, left = self._purge(control_plane=True, include_control_plane=True)
        self.assertEqual(rc, 0)
        self.assertEqual(left, ["authorized_keys.d"])

    def test_no_backup_no_purge(self):
        rc, left = self._purge(control_plane=False, include_control_plane=False,
                               backup_complete=False)
        self.assertNotEqual(rc, 0, "the step must report FAILED")
        self.assertIn("saved_nets.json", left)

    def test_backup_holds_the_nets_and_gates_the_purge(self):
        with tempfile.TemporaryDirectory() as tmp:
            etc = os.path.join(tmp, "lager")
            backup = os.path.join(tmp, "backup")
            self._tree(etc, control_plane=True)
            with open(os.path.join(etc, "saved_nets.json"), "w") as fh:
                fh.write('[{"name": "vbat"}]')
            _n, _d, cmd = u.etc_lager_backup_step(backup)
            # tar -C /etc ... lager: point -C at the temp dir's parent.
            local = _local(cmd, etc).replace("-C /etc ", f"-C {tmp} ")
            subprocess.run(["bash", "-c", local], check=True)
            self.assertTrue(os.path.exists(os.path.join(backup, ".complete")))
            with open(os.path.join(backup, "saved_nets.json")) as fh:
                self.assertIn("vbat", fh.read())
            tgz = os.path.join(backup, "etc-lager.tgz")
            self.assertEqual(os.stat(tgz).st_mode & 0o777, 0o600, "it holds secrets")


class ConfigStateParsing(unittest.TestCase):
    def _parse(self, raw):
        return u.inspect_config_state(lambda _cmd: raw)

    def test_managed_box(self):
        st = self._parse("etc=1\ncp=1\nothers=gateway,cam\nnets=7")
        self.assertEqual(st, {"etc_lager": True, "control_plane": True,
                              "other_containers": ["gateway", "cam"], "nets": 7})

    def test_unparseable_nets_is_unknown_not_zero(self):
        self.assertIsNone(self._parse("etc=1\ncp=0\nothers=\nnets=?")["nets"])

    def test_failed_query_is_none(self):
        self.assertIsNone(self._parse(None))
        self.assertIsNone(self._parse("ok"))

    def test_query_runs_on_a_real_shell(self):
        out = subprocess.run(["bash", "-c", u.CONFIG_STATE_QUERY],
                             capture_output=True, text=True).stdout
        self.assertIsNotNone(u.inspect_config_state(lambda _c: out))


class _Drive(unittest.TestCase):
    """End-to-end over a faked SSH transport. ``state`` is what the box
    reports to CONFIG_STATE_QUERY."""

    MANAGED_BOX = "etc=1\ncp=1\nothers=gateway\nnets=3"

    def drive(self, args, *, state=MANAGED_BOX, priv_ok=None):
        remotes = []

        def fake_run(cmd, **_kwargs):
            remote = cmd[-1] if isinstance(cmd, (list, tuple)) else cmd
            remotes.append(remote)
            if remote == u.CONFIG_STATE_QUERY:
                return mock.Mock(returncode=0, stdout=state, stderr="")
            if remote.startswith("cat ") and u._PRIV_RESULTS_PATH in remote:
                ok = priv_ok if priv_ok is not None else (
                    "config_backup=OK\netc_lager=OK\nlock_state=OK\n")
                return mock.Mock(returncode=0, stdout=ok, stderr="")
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        pool = mock.Mock()
        pool.ensure_connection.return_value = False
        with mock.patch.object(u.subprocess, "run", fake_run), \
                mock.patch.object(u, "get_ssh_connection_pool", return_value=pool), \
                mock.patch.object(u, "get_box_name_by_ip", return_value=None), \
                mock.patch.object(bs, "acquire_box_lock", return_value=("acquired", {})), \
                mock.patch.object(bs, "release_box_lock", return_value=True), \
                mock.patch.object(bs, "get_lock_holder", return_value="test-holder"), \
                mock.patch.object(bs, "HeartbeatThread", return_value=mock.Mock()):
            result = CliRunner().invoke(u.uninstall, ["--ip", "10.0.0.1", *args])
        return result, remotes

    def priv_session(self, remotes):
        """The one `ssh -t` payload, which carries every privileged step."""
        sessions = [r for r in remotes if u._PRIV_RESULTS_PATH in r and "if (" in r]
        self.assertEqual(len(sessions), 1, remotes)
        return sessions[0]


class DefaultKeepsConfig(_Drive):
    def test_default_only_clears_lock_state(self):
        result, remotes = self.drive(["--yes"])
        self.assertEqual(result.exit_code, 0, result.output)
        session = self.priv_session(remotes)
        self.assertIn('"lock_state=OK"', session)
        self.assertNotIn('"etc_lager=OK"', session)
        self.assertNotIn('"config_backup=OK"', session)
        self.assertIn("/etc/lager was kept", result.output)

    def test_keep_config_is_a_no_op_alias(self):
        _r1, default = self.drive(["--yes"])
        result, alias = self.drive(["--yes", "--keep-config"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.priv_session(alias), self.priv_session(default))

    def test_all_does_not_imply_purge(self):
        result, remotes = self.drive(["--yes", "--all"])
        self.assertEqual(result.exit_code, 0, result.output)
        session = self.priv_session(remotes)
        self.assertNotIn('"etc_lager=OK"', session)
        self.assertIn('"lock_state=OK"', session)
        self.assertIn('"sudoers=OK"', session)

    def test_other_containers_are_named_as_left_running(self):
        result, remotes = self.drive(["--yes"])
        self.assertIn("Left running (not lager's): gateway", result.output)
        self.assertFalse([r for r in remotes if "docker" in r and "gateway" in r
                          and r != u.CONFIG_STATE_QUERY and ("rm" in r or "stop" in r)])


class FlagErrors(_Drive):
    def test_keep_and_purge_contradict(self):
        result, remotes = self.drive(["--yes", "--keep-config", "--purge-config"])
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(remotes, [], "rejected before touching the box")

    def test_include_control_plane_needs_purge(self):
        result, remotes = self.drive(["--yes", "--include-control-plane"])
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(remotes, [])


class PurgeConfig(_Drive):
    def test_backup_runs_before_purge_in_one_session(self):
        result, remotes = self.drive(["--yes", "--purge-config"])
        self.assertEqual(result.exit_code, 0, result.output)
        session = self.priv_session(remotes)
        self.assertLess(session.index("config_backup"), session.index("etc_lager"))
        self.assertNotIn("lock_state", session, "the purge removes the lock files itself")

    def test_nets_count_and_backup_path_are_announced(self):
        result, _remotes = self.drive(["--yes", "--purge-config"])
        self.assertIn("3 saved net(s) will be deleted", result.output)
        self.assertIn("~/lager-backup-", result.output)
        self.assertIn("sudo install -o 33 -g 33 -m 644", result.output)

    def test_control_plane_files_are_kept_by_default(self):
        result, _remotes = self.drive(["--yes", "--purge-config"])
        self.assertIn("kept: control plane files", result.output)
        self.assertNotIn("drops off its control plane", result.output)

    def test_include_control_plane_warns(self):
        result, remotes = self.drive(["--yes", "--purge-config", "--include-control-plane"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("drops off its control plane", result.output)
        session = self.priv_session(remotes)
        self.assertIn("-maxdepth 1 ! -name 'authorized_keys.d' -exec", session)
        self.assertNotIn("control_plane.json", session)

    def test_failed_backup_is_reported(self):
        result, _remotes = self.drive(
            ["--yes", "--purge-config"], priv_ok="config_backup=FAIL\netc_lager=FAIL\n")
        self.assertIn("NOT purged", result.output)
        self.assertIn("FAILED", result.output)


class DryRunPlan(_Drive):
    def test_default_dry_run_says_config_is_kept(self):
        result, remotes = self.drive(["--dry-run"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("/etc/lager is kept", result.output)
        self.assertIn("Managed by a control plane: control_plane.json present", result.output)
        self.assertIn("gateway: left running", result.output)
        self.assertIn("No changes were made", result.output)
        self.assertFalse([r for r in remotes if u._PRIV_RESULTS_PATH in r])

    def test_purge_dry_run_counts_nets(self):
        result, _remotes = self.drive(["--dry-run", "--purge-config"])
        self.assertIn("saved nets that will be deleted: 3", result.output)
        self.assertIn("kept: control plane files", result.output)

    def test_include_control_plane_dry_run(self):
        result, _remotes = self.drive(["--dry-run", "--purge-config", "--include-control-plane"])
        self.assertIn("DELETED: control plane files", result.output)


class NoProductNames(unittest.TestCase):
    def test_names_the_role_not_a_product(self):
        # Lager is the open standard: it speaks of "a control plane", the
        # same rule test_box_ssh_identity pins for the key-setup modules.
        import inspect
        self.assertNotIn("stout", inspect.getsource(u).lower())


if __name__ == "__main__":
    unittest.main()
