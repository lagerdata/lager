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
            which="/usr/bin/ssh-copy-id", register=(True, ""),
            managed=False, removed=True, accepts_password=None):
    """Run `lager ssh-setup` with the helpers mocked.

    auth_sequence drives successive key_installed_on_box() return values
    (probe, then post-copy verify): True installed, False absent, None
    "could not ask". copy_results feeds mod.subprocess.run for the
    ssh-copy-id call. `managed` is what the box answers about a control
    plane; `removed` whether taking the key back out succeeded;
    `accepts_password` whether its sshd offers a password at all, with None
    meaning "could not ask" — the answer that preserves the behaviour these
    cases were written against.

    register_lager_box_key, box_has_control_plane, remove_lager_box_key and
    box_accepts_a_password MUST all be mocked here even where a test does not assert on them: they
    live in _ssh and run their own subprocess.run, which patching
    mod.subprocess does not reach — so leaving any of them live makes these
    tests open a real SSH connection to 1.2.3.4 and sit there until the
    timeout.
    """
    copy_run = RecordingRun(copy_results or [])
    auth = list(auth_sequence)
    registered = []
    removals = []

    def fake_installed(dest, **kwargs):
        return auth.pop(0)

    def fake_register(dest, **kwargs):
        registered.append(dest)
        return register

    def fake_remove(dest, **kwargs):
        removals.append(dest)
        return removed

    with patch.object(mod, "subprocess") as sub, \
         patch.object(mod, "resolve_and_validate_box", lambda ctx, box: "1.2.3.4"), \
         patch.object(mod, "resolve_box_user", lambda ip: "boxuser"), \
         patch.object(mod, "ensure_lager_box_keypair", lambda *a, **k: generated), \
         patch.object(mod, "key_installed_on_box", fake_installed), \
         patch.object(mod, "register_lager_box_key", fake_register), \
         patch.object(mod, "box_has_control_plane", lambda dest, **k: managed), \
         patch.object(mod, "box_accepts_a_password", lambda dest, **k: accepts_password), \
         patch.object(mod, "remove_lager_box_key", fake_remove), \
         patch.object(mod.shutil, "which", lambda name: which):
        sub.run = copy_run
        result = CliRunner().invoke(mod.ssh_setup, [])
    copy_run.registered = registered
    copy_run.removals = removals
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


class ABoxThatTakesNoPassword(unittest.TestCase):
    """Every hardened box sets PasswordAuthentication no.

    ssh-copy-id cannot install anything there, so the command used to promise
    a password prompt that never came and then report the failure as a wrong
    password — for a password the box would have refused however it was typed.
    On a locked-down fleet that is the ordinary case, not the edge one."""

    def test_does_not_promise_a_prompt_it_cannot_deliver(self):
        result, copy_run = _invoke(auth_sequence=[None], accepts_password=False)
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn("enter the box password", _text(result))
        # And never runs ssh-copy-id, which could only fail.
        self.assertEqual(copy_run.calls, [])

    def test_says_what_is_actually_wrong(self):
        result, _ = _invoke(auth_sequence=[None], accepts_password=False)
        self.assertIn("accepts only key authentication", _text(result))
        self.assertIn("ask an admin to grant you access", _text(result).lower())

    def test_a_box_that_takes_a_password_is_unchanged(self):
        # True and None both keep the old path: only a definite "no password"
        # is grounds for refusing to try.
        for answer in (True, None):
            with self.subTest(accepts_password=answer):
                _result, copy_run = _invoke(
                    copy_results=[_proc(0)], auth_sequence=[None, True],
                    accepts_password=answer)
                self.assertEqual(len(copy_run.calls), 1)


class ControlPlaneManagedBox(unittest.TestCase):
    """A key this command installs on a managed box is a credential the
    control plane never granted, cannot account for, and cannot revoke when
    the operator leaves. Every box `lager install` touched grew one, because
    the control-plane question was asked only after ssh-copy-id had already
    planted the key and could do nothing but choose a warning."""

    def test_a_reachable_managed_box_is_already_set_up(self):
        # False means the box answered, so an identity authenticated to ask --
        # passwordless SSH already works, using the key the control plane
        # installed. Installing a second one adds a credential and no access.
        result, copy_run = _invoke(auth_sequence=[False], managed=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(copy_run.calls, [])
        self.assertEqual(copy_run.removals, [])
        self.assertIn("no separate Lager key is needed", _text(result))
        self.assertIn("already authorized", _text(result))

    def test_never_asks_a_reachable_operator_to_file_a_second_key(self):
        # One key, registered once with the control plane, is the whole
        # contract. Telling someone who already has working access to publish
        # another key of their own breaks it.
        result, _copy_run = _invoke(auth_sequence=[False], managed=True)
        self.assertNotIn("Register this public key", _text(result))
        self.assertNotIn("lager_box.pub", _text(result))

    def test_takes_the_key_back_out_when_it_could_only_ask_afterwards(self):
        # None is "could not reach the box at all": the question has to wait
        # for the connection ssh-copy-id's password prompt buys.
        result, copy_run = _invoke(copy_results=[_proc(0)],
                                   auth_sequence=[None, True], managed=True)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(len(copy_run.calls), 1)
        self.assertEqual(copy_run.removals, ["boxuser@1.2.3.4"])
        self.assertIn("has been removed again", _text(result))
        self.assertEqual(copy_run.registered, [])
        # A grant, not a key: the operator is missing access, and handing them
        # a credential to file themselves is the thing this refuses to do.
        self.assertIn("Ask an admin to grant you access", _text(result))
        self.assertNotIn("Register this public key", _text(result))

    def test_says_so_when_the_key_could_not_be_taken_back_out(self):
        result, copy_run = _invoke(copy_results=[_proc(0)],
                                   auth_sequence=[None, True],
                                   managed=True, removed=False)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(copy_run.removals, ["boxuser@1.2.3.4"])
        self.assertIn("the removal failed", _text(result))
        self.assertNotIn("has been removed again", _text(result))

    def test_leaves_an_already_authorized_key_alone(self):
        # Pulling a working key would lock out an operator whose control-plane
        # key is not installed yet. Warn, and let registration plus the next
        # lockdown retire it.
        result, copy_run = _invoke(auth_sequence=[True], managed=True,
                                   register=(False, "Permission denied"))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(copy_run.removals, [])
        self.assertIn("outside the control plane", _text(result))
        # Says what is true, asks for nothing: the operator's lasting access is
        # the grant, and the control plane installs their key for them.
        self.assertNotIn("Register your public key", _text(result))
        self.assertNotIn("lager_box.pub", _text(result))

    def test_unmanaged_boxes_are_untouched(self):
        result, copy_run = _invoke(copy_results=[_proc(0)],
                                   auth_sequence=[False, True], managed=False)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(copy_run.removals, [])
        self.assertEqual(copy_run.registered, ["boxuser@1.2.3.4"])


if __name__ == "__main__":
    unittest.main()
