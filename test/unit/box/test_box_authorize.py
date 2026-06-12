# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
CliRunner-based tests for cli/commands/box/authorize.py.

All subprocess calls (ssh-keygen, the BatchMode probe, ssh-copy-id) are
mocked. The one behavior worth guarding hardest: ssh-copy-id must run
WITHOUT capture/stdin kwargs so its password prompt inherits the TTY.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from cli.commands.box import authorize as mod
from cli.commands.box._ssh import _KEY_FALLBACK_DESTS


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


def _invoke(run, *, key_exists=True, which="/usr/bin/ssh-copy-id"):
    with patch.object(mod, "subprocess") as sub, \
         patch.object(mod, "resolve_and_validate_box", lambda ctx, box: "1.2.3.4"), \
         patch.object(mod, "resolve_box_user", lambda ip: "boxuser"), \
         patch.object(mod.os.path, "exists", lambda p: key_exists), \
         patch.object(mod.shutil, "which", lambda name: which):
        sub.run = run
        result = CliRunner().invoke(mod.box_authorize, [])
    return result


def _text(result):
    return result.output + (result.stderr or "")


class EnsureKeypair(unittest.TestCase):
    def test_existing_key_skips_keygen(self):
        run = RecordingRun([])
        with patch.object(mod, "subprocess") as sub, \
             patch.object(mod.os.path, "exists", lambda p: True):
            sub.run = run
            self.assertFalse(mod._ensure_keypair("/tmp/nope/lager_box"))
        self.assertEqual(run.calls, [])

    def test_missing_key_runs_ssh_keygen(self):
        run = RecordingRun([_proc(0)])
        with patch.object(mod, "subprocess") as sub, \
             patch.object(mod.os.path, "exists", lambda p: False), \
             patch.object(mod.os, "makedirs", lambda *a, **k: None):
            sub.run = run
            self.assertTrue(mod._ensure_keypair("/tmp/nope/lager_box"))
        argv, kwargs = run.calls[0]
        self.assertEqual(argv[:4], ["ssh-keygen", "-t", "ed25519", "-f"])
        self.assertIn("-N", argv)
        self.assertIn("lager-box-access", argv)


class AlreadyAuthorized(unittest.TestCase):
    def test_short_circuits_before_ssh_copy_id(self):
        run = RecordingRun([_proc(0)])  # probe succeeds
        result = _invoke(run)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("already authorized", _text(result))
        # Only the probe ran; ssh-copy-id never invoked.
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(run.calls[0][0][0], "ssh")

    def test_clears_key_fallback_dest(self):
        dest = "boxuser@1.2.3.4"
        _KEY_FALLBACK_DESTS.add(dest)
        try:
            result = _invoke(RecordingRun([_proc(0)]))
            self.assertEqual(result.exit_code, 0)
            self.assertNotIn(dest, _KEY_FALLBACK_DESTS)
        finally:
            _KEY_FALLBACK_DESTS.discard(dest)


class CopyFlow(unittest.TestCase):
    def test_copy_then_verify_success(self):
        # probe fails, ssh-copy-id succeeds, verify probe succeeds
        run = RecordingRun([_proc(1), _proc(0), _proc(0)])
        result = _invoke(run)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Success", _text(result))
        argv, kwargs = run.calls[1]
        self.assertEqual(argv[0], "ssh-copy-id")
        self.assertIn("-i", argv)
        self.assertTrue(argv[-1].endswith("boxuser@1.2.3.4"))
        # Must inherit the TTY: no capture_output/text/input kwargs.
        self.assertEqual(kwargs, {})

    def test_ssh_copy_id_failure_reports_retry(self):
        run = RecordingRun([_proc(1), _proc(1)])
        result = _invoke(run)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ssh-copy-id", _text(result))
        self.assertIn("Retry manually", _text(result))

    def test_verify_failure_after_copy(self):
        run = RecordingRun([_proc(1), _proc(0), _proc(1)])
        result = _invoke(run)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("still fails", _text(result))

    def test_missing_ssh_copy_id_binary(self):
        run = RecordingRun([_proc(1)])  # probe fails
        result = _invoke(run, which=None)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ssh-copy-id was not found", _text(result))


if __name__ == "__main__":
    unittest.main()
