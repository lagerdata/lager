# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Tests for `lager ssh-setup` and the shared key-provisioning helpers it
uses from cli/commands/box/_ssh.py.

The keygen/probe logic lives in _ssh (shared with `lager update`); the
command itself only orchestrates resolve -> ensure key -> probe ->
ssh-copy-id -> verify. The behavior worth guarding hardest: ssh-copy-id
must run WITHOUT capture/stdin kwargs so its password prompt inherits
the TTY.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from cli.commands.box import ssh_setup as mod
from cli.commands.box import _ssh
from cli.commands.box._ssh import _KEY_FALLBACK_DESTS
from cli.errors import LagerError


def _proc(rc, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


class RecordingRun:
    """Replaces subprocess.run; replays canned results and records calls."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.results.pop(0)


# ---------------------------------------------------------------------------
# Shared helper: ensure_lager_box_keypair (in _ssh)
# ---------------------------------------------------------------------------

class EnsureKeypair(unittest.TestCase):
    """Driven with real paths in a temp dir: ensure_lager_box_keypair takes
    the key path as a parameter, so nothing about the filesystem needs to be
    faked. The old global os.path.exists patch reached far past the function
    under test — it made shutil.which lie too, so the keygen-failure case
    was passing on the "ssh-keygen not found" guard without keygen ever
    running. shutil.which stays pinned so the tests don't depend on
    ssh-keygen being installed on the machine running them."""

    def test_existing_key_skips_keygen(self):
        run = RecordingRun([])
        with tempfile.TemporaryDirectory() as td:
            key = os.path.join(td, "lager_box")
            open(key, "w", encoding="utf-8").close()
            with patch.object(_ssh, "subprocess") as sub:
                sub.run = run
                self.assertFalse(_ssh.ensure_lager_box_keypair(key))
        self.assertEqual(run.calls, [])

    def test_missing_key_runs_ssh_keygen(self):
        run = RecordingRun([_proc(0)])
        with tempfile.TemporaryDirectory() as td:
            key = os.path.join(td, "keys", "lager_box")
            with patch.object(_ssh, "subprocess") as sub, \
                 patch.object(_ssh.shutil, "which", lambda _: "ssh-keygen"):
                sub.run = run
                self.assertTrue(_ssh.ensure_lager_box_keypair(key))
            self.assertTrue(os.path.isdir(os.path.dirname(key)),
                            "the key's directory is created for keygen")
        argv, _kwargs = run.calls[0]
        self.assertEqual(argv[:4], ["ssh-keygen", "-t", "ed25519", "-f"])
        self.assertIn("-N", argv)
        self.assertIn("lager-box-access", argv)

    def test_keygen_failure_raises_lager_error(self):
        run = RecordingRun([_proc(1, stderr="disk full")])
        with tempfile.TemporaryDirectory() as td:
            key = os.path.join(td, "keys", "lager_box")
            with patch.object(_ssh, "subprocess") as sub, \
                 patch.object(_ssh.shutil, "which", lambda _: "ssh-keygen"):
                sub.run = run
                with self.assertRaises(LagerError):
                    _ssh.ensure_lager_box_keypair(key)
        self.assertEqual(len(run.calls), 1,
                         "the failure must come from keygen itself, not an "
                         "earlier guard")


# ---------------------------------------------------------------------------
# `lager ssh-setup` command orchestration
# ---------------------------------------------------------------------------

def _invoke(*, copy_results=None, generated=False, auth_sequence=(),
            which="/usr/bin/ssh-copy-id", register=(True, "")):
    """Run `lager ssh-setup` with the helpers mocked.

    auth_sequence drives successive key_installed_on_box() return values
    (probe, then post-copy verify): True installed, False absent, None
    "could not ask". copy_results feeds mod.subprocess.run for the
    ssh-copy-id call.

    register_lager_box_key MUST be mocked here even where a test does not
    assert on it: it lives in _ssh and runs its own subprocess.run, which
    patching mod.subprocess does not reach — so leaving it live makes these
    tests open a real SSH connection to 1.2.3.4 and sit there until the
    30s timeout.
    """
    copy_run = RecordingRun(copy_results or [])
    auth = list(auth_sequence)
    registered = []

    def fake_installed(dest, **kwargs):
        return auth.pop(0)

    def fake_register(dest, **kwargs):
        registered.append(dest)
        return register

    with patch.object(mod, "subprocess") as sub, \
         patch.object(mod, "resolve_and_validate_box", lambda ctx, box: "1.2.3.4"), \
         patch.object(mod, "resolve_box_user", lambda ip: "boxuser"), \
         patch.object(mod, "ensure_lager_box_keypair", lambda *a, **k: generated), \
         patch.object(mod, "key_installed_on_box", fake_installed), \
         patch.object(mod, "register_lager_box_key", fake_register), \
         patch.object(mod.shutil, "which", lambda name: which):
        sub.run = copy_run
        result = CliRunner().invoke(mod.ssh_setup, [])
    copy_run.registered = registered
    return result, copy_run


def _text(result):
    return result.output + (result.stderr or "")


class AlreadyAuthorized(unittest.TestCase):
    def test_short_circuits_before_ssh_copy_id(self):
        result, copy_run = _invoke(auth_sequence=[True])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("already authorized", _text(result))
        # ssh-copy-id never invoked.
        self.assertEqual(copy_run.calls, [])

    def test_clears_key_fallback_dest(self):
        dest = "boxuser@1.2.3.4"
        _KEY_FALLBACK_DESTS.add(dest)
        try:
            result, _ = _invoke(auth_sequence=[True])
            self.assertEqual(result.exit_code, 0)
            self.assertNotIn(dest, _KEY_FALLBACK_DESTS)
        finally:
            _KEY_FALLBACK_DESTS.discard(dest)


class CopyFlow(unittest.TestCase):
    def test_copy_then_verify_success(self):
        # probe fails, ssh-copy-id succeeds, verify probe succeeds
        result, copy_run = _invoke(copy_results=[_proc(0)],
                                   auth_sequence=[False, True])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Success", _text(result))
        argv, kwargs = copy_run.calls[0]
        self.assertEqual(argv[0], "ssh-copy-id")
        self.assertIn("-i", argv)
        self.assertTrue(argv[-1].endswith("boxuser@1.2.3.4"))
        # Must inherit the TTY: no capture_output/text/input kwargs.
        self.assertEqual(kwargs, {})

    def test_forces_the_copy_past_ssh_copy_ids_own_filter(self):
        """ssh-copy-id decides "already installed?" by logging in with the
        key -- which succeeds on any identity ssh offers, including an
        ssh_config `Host *` IdentityFile. Observed on hardware: it reported
        "All keys were skipped because they already exist on the remote
        system" for a box that then rejected the key under
        `ssh -F /dev/null`. -f skips that filter; the caller has already
        established absence by grepping authorized_keys, which is exact."""
        _result, copy_run = _invoke(copy_results=[_proc(0)],
                                    auth_sequence=[False, True])
        argv, _kwargs = copy_run.calls[0]
        self.assertIn("-f", argv)

    def test_ssh_copy_id_failure_reports_retry(self):
        result, _ = _invoke(copy_results=[_proc(1)], auth_sequence=[False])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ssh-copy-id", _text(result))
        self.assertIn("Retry manually", _text(result))

    def test_verify_failure_after_copy(self):
        result, _ = _invoke(copy_results=[_proc(0)], auth_sequence=[False, False])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not in the box's authorized_keys", _text(result))

    def test_missing_ssh_copy_id_binary(self):
        result, copy_run = _invoke(auth_sequence=[False], which=None)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ssh-copy-id was not found", _text(result))
        self.assertEqual(copy_run.calls, [])


class KeyRegistration(unittest.TestCase):
    """ssh-copy-id appends OUTSIDE every marker block, so the key it installs
    is dropped by any key manager that rebuilds authorized_keys from its own
    source. Registering the public half in /etc/lager/authorized_keys.d is
    what makes it survive — start_box.sh rebuilds its block from there."""

    def test_registers_after_a_successful_copy(self):
        result, copy_run = _invoke(copy_results=[_proc(0)],
                                   auth_sequence=[False, True])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(copy_run.registered, ["boxuser@1.2.3.4"])

    def test_registers_on_an_already_authorized_box(self):
        # The repair path: a box whose key was installed before registration
        # existed gets fixed by re-running ssh-setup, with no prompt.
        result, copy_run = _invoke(auth_sequence=[True])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(copy_run.registered, ["boxuser@1.2.3.4"])

    def test_registration_failure_warns_but_does_not_fail(self):
        # The key is installed and working at this point; the command's
        # headline job is done.
        result, _copy_run = _invoke(
            copy_results=[_proc(0)], auth_sequence=[False, True],
            register=(False, "Permission denied"),
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("did not register", _text(result))
        self.assertIn("Permission denied", _text(result))
        self.assertIn("Success", _text(result))

    def test_no_registration_when_the_key_never_got_installed(self):
        result, copy_run = _invoke(copy_results=[_proc(1)], auth_sequence=[False])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(copy_run.registered, [])


if __name__ == "__main__":
    unittest.main()
