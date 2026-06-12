# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for cli/commands/box/_ssh.py.

Drives the key-selection and auth-fallback logic by mocking subprocess.run.
No real ssh, no network.
"""

import unittest
from unittest.mock import patch

from cli.commands.box import _ssh


def _proc(rc, stdout="", stderr=""):
    class P:
        pass
    p = P()
    p.returncode = rc
    p.stdout = stdout
    p.stderr = stderr
    return p


_AUTH_DENIED = _proc(255, "", "boxuser@192.0.2.7: Permission denied (publickey,password).\r\n")


class _RunnerCase(unittest.TestCase):
    """Common patching: lager_box key exists, box record resolves to boxuser."""

    KEY_EXISTS = True

    def setUp(self):
        _ssh._KEY_FALLBACK_DESTS.clear()
        patches = [
            patch("cli.commands.box._ssh.os.path.exists", return_value=self.KEY_EXISTS),
            patch("cli.box_storage.get_box_name_by_ip", return_value="test-box"),
            patch("cli.box_storage.get_box_user", return_value="boxuser"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _ssh_args(self, run_mock, call_index=0):
        return run_mock.call_args_list[call_index][0][0]


class LagerBoxKeyUsage(_RunnerCase):
    def test_key_file_present_adds_dash_i(self):
        with patch("cli.commands.box._ssh.subprocess.run", return_value=_proc(0, "ok")) as run:
            rc, out, _ = _ssh.default_ssh_runner("192.0.2.7", "true")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "ok")
        args = self._ssh_args(run)
        self.assertIn("-i", args)
        self.assertIn(_ssh._LAGER_BOX_KEY, args)
        self.assertIn("boxuser@192.0.2.7", args)

    def test_remote_command_failure_does_not_retry(self):
        # rc 1 is the REMOTE command failing (e.g. stat on a missing path);
        # transport worked, so a retry would re-run the command for nothing.
        with patch("cli.commands.box._ssh.subprocess.run", return_value=_proc(1, "", "no such file")) as run:
            rc, _, _ = _ssh.default_ssh_runner("192.0.2.7", "stat /nope")
        self.assertEqual(rc, 1)
        self.assertEqual(run.call_count, 1)


class NoLagerBoxKey(_RunnerCase):
    KEY_EXISTS = False

    def test_no_key_file_omits_dash_i(self):
        with patch("cli.commands.box._ssh.subprocess.run", return_value=_proc(0)) as run:
            _ssh.default_ssh_runner("192.0.2.7", "true")
        self.assertNotIn("-i", self._ssh_args(run))

    def test_auth_failure_without_key_does_not_retry(self):
        # Nothing to fall back to: the default identities were already offered.
        with patch("cli.commands.box._ssh.subprocess.run", return_value=_AUTH_DENIED) as run:
            rc, _, _ = _ssh.default_ssh_runner("192.0.2.7", "true")
        self.assertEqual(rc, 255)
        self.assertEqual(run.call_count, 1)


class AuthFailureRetry(_RunnerCase):
    def test_permission_denied_retries_without_key(self):
        with patch(
            "cli.commands.box._ssh.subprocess.run",
            side_effect=[_AUTH_DENIED, _proc(0, "33:33\n")],
        ) as run:
            rc, out, _ = _ssh.default_ssh_runner("192.0.2.7", "stat -c %u:%g /x")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "33:33\n")
        self.assertEqual(run.call_count, 2)
        self.assertIn("-i", self._ssh_args(run, 0))
        self.assertNotIn("-i", self._ssh_args(run, 1))

    def test_too_many_auth_failures_retries(self):
        denied = _proc(255, "", "Received disconnect: Too many authentication failures\r\n")
        with patch(
            "cli.commands.box._ssh.subprocess.run",
            side_effect=[denied, _proc(0)],
        ) as run:
            rc, _, _ = _ssh.default_ssh_runner("192.0.2.7", "true")
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 2)

    def test_connect_timeout_does_not_retry(self):
        # rc 255 from a dead box; retrying would just hang a second time.
        timeout = _proc(255, "", "ssh: connect to host 192.0.2.7 port 22: Connection timed out\r\n")
        with patch("cli.commands.box._ssh.subprocess.run", return_value=timeout) as run:
            rc, _, _ = _ssh.default_ssh_runner("192.0.2.7", "true")
        self.assertEqual(rc, 255)
        self.assertEqual(run.call_count, 1)

    def test_retry_failure_returns_second_result(self):
        with patch(
            "cli.commands.box._ssh.subprocess.run",
            side_effect=[_AUTH_DENIED, _AUTH_DENIED],
        ) as run:
            rc, _, stderr = _ssh.default_ssh_runner("192.0.2.7", "true")
        self.assertEqual(rc, 255)
        self.assertEqual(run.call_count, 2)
        self.assertIn("Permission denied", stderr)

    def test_stdin_preserved_on_retry(self):
        with patch(
            "cli.commands.box._ssh.subprocess.run",
            side_effect=[_AUTH_DENIED, _proc(0)],
        ) as run:
            _ssh.default_ssh_runner("192.0.2.7", "sudo tee /etc/x", stdin="conf body")
        for call in run.call_args_list:
            self.assertEqual(call[1]["input"], "conf body")

    def test_mixed_case_permission_denied_retries(self):
        denied = _proc(255, "", "boxuser@192.0.2.7: Permission Denied (publickey).\r\n")
        with patch(
            "cli.commands.box._ssh.subprocess.run",
            side_effect=[denied, _proc(0)],
        ) as run:
            rc, _, _ = _ssh.default_ssh_runner("192.0.2.7", "true")
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 2)

    def test_multiline_banner_stderr_retries(self):
        # Boxes with a /etc/issue.net banner prepend it to stderr; the auth
        # marker is on a later line.
        denied = _proc(255, "", "Authorized use only.\nThis system is monitored.\nboxuser@192.0.2.7: Permission denied (publickey,password).\r\n")
        with patch(
            "cli.commands.box._ssh.subprocess.run",
            side_effect=[denied, _proc(0)],
        ) as run:
            rc, _, _ = _ssh.default_ssh_runner("192.0.2.7", "true")
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 2)

    def test_timeout_kwarg_forwarded_on_retry(self):
        with patch(
            "cli.commands.box._ssh.subprocess.run",
            side_effect=[_AUTH_DENIED, _proc(0)],
        ) as run:
            _ssh.default_ssh_runner("192.0.2.7", "true", timeout=7)
        for call in run.call_args_list:
            self.assertEqual(call[1]["timeout"], 7)

    def test_memo_is_per_destination(self):
        # A fallback on box A must not strip the key from calls to box B.
        with patch("cli.box_storage.get_box_name_by_ip", return_value=None), \
             patch(
                 "cli.commands.box._ssh.subprocess.run",
                 side_effect=[
                     _proc(255, "", "lagerdata@10.0.0.1: Permission denied (publickey).\r\n"),
                     _proc(0),  # fallback for 10.0.0.1
                     _proc(0),  # first (keyed) call for 10.0.0.2
                 ],
             ) as run:
            _ssh.default_ssh_runner("10.0.0.1", "true")
            _ssh.default_ssh_runner("10.0.0.2", "true")
        self.assertEqual(run.call_count, 3)
        self.assertIn("-i", self._ssh_args(run, 2))

    def test_fallback_memo_skips_key_on_next_call(self):
        with patch(
            "cli.commands.box._ssh.subprocess.run",
            side_effect=[_AUTH_DENIED, _proc(0), _proc(0)],
        ) as run:
            _ssh.default_ssh_runner("192.0.2.7", "true")
            _ssh.default_ssh_runner("192.0.2.7", "true")
        # Call 1: keyed attempt + keyless retry. Call 2: keyless only.
        self.assertEqual(run.call_count, 3)
        self.assertNotIn("-i", self._ssh_args(run, 2))


class HungConnection(_RunnerCase):
    """A hung ssh (TimeoutExpired) must surface as a clean rc-255 tuple,
    never a traceback — apply/mount add call this from deep in the pipeline."""

    def test_timeout_returns_255_tuple_not_exception(self):
        import subprocess as sp
        with patch(
            "cli.commands.box._ssh.subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="ssh", timeout=60),
        ) as run:
            rc, out, stderr = _ssh.default_ssh_runner("192.0.2.7", "true")
        self.assertEqual(rc, 255)
        self.assertEqual(out, "")
        self.assertIn("timed out after 60s", stderr)
        self.assertIn("boxuser@192.0.2.7", stderr)
        # Not an auth failure -> no keyless retry (it would just hang again).
        self.assertEqual(run.call_count, 1)

    def test_custom_timeout_in_message(self):
        import subprocess as sp
        with patch(
            "cli.commands.box._ssh.subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="ssh", timeout=5),
        ):
            _, _, stderr = _ssh.default_ssh_runner("192.0.2.7", "true", timeout=5)
        self.assertIn("timed out after 5s", stderr)


class ResolveBoxUser(unittest.TestCase):
    def test_custom_user_from_box_record(self):
        with patch("cli.box_storage.get_box_name_by_ip", return_value="test-box"), \
             patch("cli.box_storage.get_box_user", return_value="boxuser"):
            self.assertEqual(_ssh.resolve_box_user("192.0.2.7"), "boxuser")

    def test_unknown_ip_defaults_to_lagerdata(self):
        with patch("cli.box_storage.get_box_name_by_ip", return_value=None):
            self.assertEqual(_ssh.resolve_box_user("10.0.0.1"), "lagerdata")


if __name__ == "__main__":
    unittest.main()
