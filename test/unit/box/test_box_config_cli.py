# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
CliRunner-based tests for cli/commands/box/config.py.

Covers the behavior changes in PR2:
  * `mount add` runs prep BEFORE persisting the JSON entry (issue 1).
  * `apply` polls the box API for readiness before set-applied-hash (issue 2).
  * `apply` rolls back to the last applied snapshot when a bounce of a new
    config fails (issue 3).

The box-side shim is mocked at `_run_box_config_py` so each test is a pure
CLI exercise — no SSH, no HTTP, no filesystem on `/etc/lager`.
"""
import json
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from cli.commands.box import config as box_config_cli


class FakeBoxBackend:
    """Replays JSON responses for the box-side shim and records calls.

    Mirrors the protocol of `_run_box_config_py`: each call corresponds to a
    box-side subcommand (validate, hash, mount-add, set-applied-hash, etc.).
    Responses are looked up by command verb so tests don't have to maintain
    an exact-order list across unrelated calls.
    """

    def __init__(self, responses=None):
        # responses[verb] is a list popped front-to-back; final entry repeats.
        self.responses = {k: list(v) for k, v in (responses or {}).items()}
        self.calls = []  # list of (verb, args_tuple)

    def register(self, verb, payload):
        self.responses.setdefault(verb, []).append(payload)

    def __call__(self, ctx, box, *args):
        verb = args[0] if args else ""
        self.calls.append((verb, args[1:]))
        if verb not in self.responses or not self.responses[verb]:
            raise AssertionError(
                f"unexpected box call: {verb!r} args={args[1:]!r}; "
                f"registered verbs: {list(self.responses)}"
            )
        bucket = self.responses[verb]
        payload = bucket[0] if len(bucket) == 1 else bucket.pop(0)
        return json.dumps(payload)


def _patch_resolve(box_value="1.2.3.4"):
    return patch.object(box_config_cli, "_resolve_box", return_value=box_value)


# ---------------------------------------------------------------------------
# Issue 1: mount add runs prep before persisting JSON
# ---------------------------------------------------------------------------

class MountAddPrepThenPersist(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def _make_prep(self, ok, action="ok", message="", manual_fix=None):
        from cli.commands.box._mount_prep import PrepResult
        return PrepResult(
            ok=ok,
            action=action,
            host_path="/Hyphen",
            message=message,
            manual_fix=manual_fix,
        )

    def test_prep_failure_prevents_json_write(self):
        backend = FakeBoxBackend()  # mount-add intentionally NOT registered
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch("cli.commands.box._mount_prep.ensure_host_path_owned",
                   return_value=self._make_prep(
                       ok=False,
                       action="refused_populated",
                       message="/Hyphen is owned by 1000:1000 and contains files.",
                       manual_fix="sudo chown -R 33:33 /Hyphen",
                   )):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["mount", "add", "/Hyphen", "/Hyphen", "--box", "HYP-3"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Mount NOT added", result.output)
        # The mount-add verb must NEVER have hit the box.
        self.assertNotIn("mount-add", [c[0] for c in backend.calls])

    def test_prep_success_then_json_write(self):
        backend = FakeBoxBackend({"mount-add": [{"ok": True}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch("cli.commands.box._mount_prep.ensure_host_path_owned",
                   return_value=self._make_prep(
                       ok=True,
                       action="created",
                       message="Created /Hyphen and chowned to 33:33.",
                   )):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["mount", "add", "/Hyphen", "/Hyphen", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Added mount /Hyphen -> /Hyphen", result.output)
        self.assertIn("mount-add", [c[0] for c in backend.calls])

    def test_no_auto_prep_skips_prep_and_persists_directly(self):
        backend = FakeBoxBackend({"mount-add": [{"ok": True}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch("cli.commands.box._mount_prep.ensure_host_path_owned") as prep_call:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["mount", "add", "/Hyphen", "/Hyphen", "--no-auto-prep", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        prep_call.assert_not_called()
        self.assertIn("mount-add", [c[0] for c in backend.calls])


# ---------------------------------------------------------------------------
# Issue 2: apply polls API readiness before set-applied-hash
# ---------------------------------------------------------------------------

class ApplyReadinessPolling(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def _backend_for_apply(self, cur_hash="aaa", applied_hash="bbb"):
        return FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": cur_hash}],
            "applied-hash": [{"hash": applied_hash}],
            "show": [{"version": 1, "mounts": []}],  # _preflight_mounts
            "set-applied-hash": [{"ok": True}],
        })

    def test_set_applied_hash_called_only_after_api_ready(self):
        backend = self._backend_for_apply()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True) as wait_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Applied box config", result.output)
        wait_mock.assert_called_once()
        verbs = [c[0] for c in backend.calls]
        self.assertIn("set-applied-hash", verbs)

    def test_api_timeout_skips_set_applied_hash(self):
        backend = self._backend_for_apply()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=False):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("didn't come up", result.output)
        verbs = [c[0] for c in backend.calls]
        self.assertNotIn("set-applied-hash", verbs)


class WaitForBoxApi(unittest.TestCase):
    """The polling helper itself — verified with injected clock/sleeper so
    the test runs in microseconds rather than 30 wall-clock seconds."""

    def test_returns_true_on_first_probe(self):
        ok = box_config_cli._wait_for_box_api(
            "1.2.3.4",
            sleeper=lambda _: None,
            clock=iter([0.0]).__next__,
            is_responding=lambda _: True,
        )
        self.assertTrue(ok)

    def test_returns_true_after_a_few_misses(self):
        responses = iter([False, False, True])
        clock_ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
        ok = box_config_cli._wait_for_box_api(
            "1.2.3.4",
            deadline_seconds=30,
            sleeper=lambda _: None,
            clock=lambda: next(clock_ticks),
            is_responding=lambda _: next(responses),
        )
        self.assertTrue(ok)

    def test_returns_false_at_deadline(self):
        # Clock jumps past the deadline before we ever get a True.
        clock_ticks = iter([0.0, 100.0])
        ok = box_config_cli._wait_for_box_api(
            "1.2.3.4",
            deadline_seconds=30,
            sleeper=lambda _: None,
            clock=lambda: next(clock_ticks),
            is_responding=lambda _: False,
        )
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# Issue 3: rollback on bounce failure
# ---------------------------------------------------------------------------

class ApplyRollback(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def _backend(self, *, restore_ok):
        return FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "show": [{"version": 1, "mounts": []}],
            "restore-applied": [
                {"ok": True} if restore_ok else
                {"ok": False, "error": "no applied snapshot available"}
            ],
        })

    def test_rollback_succeeds_when_snapshot_exists(self):
        backend = self._backend(restore_ok=True)
        # First bounce: fail. Second bounce (rollback): succeed.
        bounce = patch.object(
            box_config_cli, "_bounce_container", side_effect=[False, True],
        )
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             bounce as bounce_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Rolled back", result.output)
        self.assertEqual(bounce_mock.call_count, 2)
        verbs = [c[0] for c in backend.calls]
        self.assertIn("restore-applied", verbs)
        # Critical: applied_hash must NOT have been updated to the (rejected)
        # new config's hash.
        self.assertNotIn("set-applied-hash", verbs)

    def test_no_snapshot_no_rollback(self):
        backend = self._backend(restore_ok=False)
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=False) as bounce_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("rollback was not possible", result.output)
        # We try restore once; bounce only fired once (the original).
        self.assertEqual(bounce_mock.call_count, 1)
        verbs = [c[0] for c in backend.calls]
        self.assertNotIn("set-applied-hash", verbs)

    def test_rollback_bounce_also_fails(self):
        backend = self._backend(restore_ok=True)
        # Both bounces fail.
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=False) as bounce_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("rollback was not possible", result.output)
        self.assertEqual(bounce_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
