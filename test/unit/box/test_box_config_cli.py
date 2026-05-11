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
             patch("cli.commands.box.config.ensure_host_path_owned",
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
             patch("cli.commands.box.config.ensure_host_path_owned",
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
             patch("cli.commands.box.config.ensure_host_path_owned") as prep_call:
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
        # `show` is consulted twice during apply: once by _preflight_mounts and
        # once by the apt/sysctl host-side helpers. The single registered
        # response is reused for both calls.
        return FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": cur_hash}],
            "applied-hash": [{"hash": applied_hash}],
            "show": [{"version": 1, "mounts": []}],
            "applied-show": [None],
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
            "applied-show": [None],
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


# ---------------------------------------------------------------------------
# apt / sysctl / cargo CLI verbs
# ---------------------------------------------------------------------------

class AptCli(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_add_bad_name_round_trips_shim_error(self):
        # Host-side format validation was removed (Task B in the cleanup
        # doc). Bad input now reaches the shim, which rejects it; the host
        # CLI renders the shim's error response.
        backend = FakeBoxBackend({
            "apt-add": [{"ok": False, "errors": ["'Bad Name': invalid Debian package name (must match [a-z0-9][a-z0-9+-.]*)"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apt", "add", "Bad Name", "--box", "HYP-3"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Failed to add apt packages", result.output)
        self.assertIn("invalid Debian package name", result.output)
        self.assertIn("apt-add", [c[0] for c in backend.calls])

    def test_add_happy_path_hits_box(self):
        backend = FakeBoxBackend({"apt-add": [{"ok": True, "added": ["tcpdump"]}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apt", "add", "tcpdump", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Added 1 apt package", result.output)
        self.assertIn("apt-add", [c[0] for c in backend.calls])

    def test_remove_round_trips(self):
        backend = FakeBoxBackend({"apt-remove": [{"ok": True, "removed": ["tcpdump"]}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apt", "remove", "tcpdump", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Removed 1 apt package", result.output)

    def test_list_renders_packages(self):
        backend = FakeBoxBackend({
            "show": [{"version": 1, "apt_packages": ["tcpdump", "iptables-persistent"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apt", "list", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("tcpdump", result.output)
        self.assertIn("iptables-persistent", result.output)


class SysctlCli(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_set_requires_key_value(self):
        backend = FakeBoxBackend()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["sysctl", "set", "no_equals_here", "--box", "HYP-3"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("expected key=value", result.output)
        self.assertEqual(backend.calls, [])

    def test_set_bad_key_round_trips_shim_error(self):
        # Host-side key-format validation removed (Task B); the `key=value`
        # input split is still host-side because the shim takes a JSON
        # object, not raw `key=value`.
        backend = FakeBoxBackend({
            "sysctl-set": [{"ok": False, "errors": ["'bad-key': invalid sysctl key (must match [a-zA-Z][a-zA-Z0-9_.]*)"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["sysctl", "set", "bad-key=1", "--box", "HYP-3"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Failed to set sysctl values", result.output)
        self.assertIn("invalid sysctl key", result.output)
        self.assertIn("sysctl-set", [c[0] for c in backend.calls])

    def test_set_round_trip(self):
        backend = FakeBoxBackend({
            "sysctl-set": [{"ok": True, "set": ["net.ipv4.ip_forward"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["sysctl", "set", "net.ipv4.ip_forward=1", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Set 1 sysctl key", result.output)
        # Confirm the JSON payload was constructed correctly
        verb, args = backend.calls[-1]
        self.assertEqual(verb, "sysctl-set")
        sent = json.loads(args[0])
        self.assertEqual(sent, {"entries": {"net.ipv4.ip_forward": "1"}})

    def test_unset_round_trip(self):
        backend = FakeBoxBackend({
            "sysctl-unset": [{"ok": True, "removed": ["net.ipv4.ip_forward"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["sysctl", "unset", "net.ipv4.ip_forward", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Removed 1 sysctl key", result.output)


class CargoCli(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_add_uppercase_round_trips_shim_error(self):
        # Host-side format validation removed (Task B).
        backend = FakeBoxBackend({
            "cargo-add": [{"ok": False, "errors": ["'Bad-Crate': invalid cargo crate spec (must match [a-z0-9][a-z0-9_-]*(@version)?)"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["cargo", "add", "Bad-Crate", "--box", "HYP-3"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Failed to add cargo crates", result.output)
        self.assertIn("invalid cargo crate spec", result.output)
        self.assertIn("cargo-add", [c[0] for c in backend.calls])

    def test_add_accepts_at_version(self):
        backend = FakeBoxBackend({
            "cargo-add": [{"ok": True, "added": ["defmt-print@0.3.13"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["cargo", "add", "defmt-print@0.3.13", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Added 1 cargo crate", result.output)


class NpmCli(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_add_uppercase_round_trips_shim_error(self):
        # Host-side format validation removed (Task B).
        backend = FakeBoxBackend({
            "npm-add": [{"ok": False, "errors": ["'Express': invalid npm package spec (must be lowercase name, optional @scope/, optional @version)"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["npm", "add", "Express", "--box", "HYP-3"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Failed to add npm packages", result.output)
        self.assertIn("invalid npm package spec", result.output)
        self.assertIn("npm-add", [c[0] for c in backend.calls])

    def test_add_accepts_scoped_at_version(self):
        backend = FakeBoxBackend({
            "npm-add": [{"ok": True, "added": ["@types/node@20.0.0"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["npm", "add", "@types/node@20.0.0", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Added 1 npm package", result.output)
        verb, args = backend.calls[-1]
        self.assertEqual(verb, "npm-add")
        sent = json.loads(args[0])
        self.assertEqual(sent, {"packages": ["@types/node@20.0.0"]})

    def test_remove_round_trips(self):
        backend = FakeBoxBackend({
            "npm-remove": [{"ok": True, "removed": ["lodash"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["npm", "remove", "lodash", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Removed 1 npm package", result.output)

    def test_list_renders_packages(self):
        backend = FakeBoxBackend({
            "show": [{"version": 1, "npm_packages": ["express", "@types/node"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["npm", "list", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("express", result.output)
        self.assertIn("@types/node", result.output)


# ---------------------------------------------------------------------------
# apply: ensure_apt_packages + ensure_sysctl wiring
# ---------------------------------------------------------------------------

class ApplyHostSideOrdering(unittest.TestCase):
    """apt + sysctl run before the bounce, only when the field changed,
    and a failure aborts apply before the container restart."""

    def setUp(self):
        self.runner = CliRunner()

    def _backend(self, *, current, applied):
        return FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "show": [current],
            "applied-show": [applied],
            "set-applied-hash": [{"ok": True}],
        })

    def test_apt_skipped_when_unchanged(self):
        current = {"version": 1, "apt_packages": ["tcpdump"], "sysctl": {}, "mounts": []}
        backend = self._backend(current=current, applied=current)
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.apt_install") as apt_mock, \
             patch("cli.commands.box.config.sysctl_apply") as sysctl_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        apt_mock.assert_not_called()
        sysctl_mock.assert_not_called()
        self.assertIn("Apt packages unchanged", result.output)

    def test_apt_runs_when_changed(self):
        from cli.commands.box._host_ops import HostOpResult
        current = {
            "version": 1,
            "apt_packages": ["tcpdump", "iptables-persistent"],
            "sysctl": {},
            "mounts": [],
        }
        applied = {"version": 1, "apt_packages": ["tcpdump"], "sysctl": {}, "mounts": []}
        backend = self._backend(current=current, applied=applied)
        apt_result = HostOpResult(ok=True, action="installed", message="Installed/verified 2 apt package(s).")
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.apt_install", return_value=apt_result) as apt_mock, \
             patch("cli.commands.box.config.sysctl_apply") as sysctl_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        apt_mock.assert_called_once()
        # sysctl was unchanged (both empty), so still skipped.
        sysctl_mock.assert_not_called()

    def test_apt_failure_aborts_before_bounce(self):
        from cli.commands.box._host_ops import HostOpResult
        current = {"version": 1, "apt_packages": ["tcpdump"], "sysctl": {}, "mounts": []}
        applied = {"version": 1, "apt_packages": [], "sysctl": {}, "mounts": []}
        backend = self._backend(current=current, applied=applied)
        apt_result = HostOpResult(ok=False, action="failed", message="sudo not configured")
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container") as bounce_mock, \
             patch("cli.commands.box.config.apt_install", return_value=apt_result):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("apt install failed", result.output)
        bounce_mock.assert_not_called()
        verbs = [c[0] for c in backend.calls]
        self.assertNotIn("set-applied-hash", verbs)

    def test_sysctl_runs_when_changed(self):
        from cli.commands.box._host_ops import HostOpResult
        current = {
            "version": 1,
            "apt_packages": [],
            "sysctl": {"net.ipv4.ip_forward": "1"},
            "mounts": [],
        }
        applied = {"version": 1, "apt_packages": [], "sysctl": {}, "mounts": []}
        backend = self._backend(current=current, applied=applied)
        sysctl_result = HostOpResult(ok=True, action="applied", message="Wrote 1 sysctl key.")
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.sysctl_apply", return_value=sysctl_result) as sysctl_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        sysctl_mock.assert_called_once()
        # sysctl_apply got the values from the current config.
        called_with = sysctl_mock.call_args[0][1]
        self.assertEqual(called_with, {"net.ipv4.ip_forward": "1"})


class PostApplyConsistency(unittest.TestCase):
    """The post-bounce safety net. After a successful bounce we re-read the
    box's view of the config; if it differs from the snapshot we bounced
    against, the file was hand-edited mid-apply and the container is running
    the older version. We warn and skip applied-hash so the next apply picks
    up the new edits."""

    def setUp(self):
        self.runner = CliRunner()

    def _backend(self, *, pre_show, post_show, post_validate):
        # apply_cmd reads `show` twice pre-bounce (preflight + current_show
        # capture) and once post-bounce in the consistency check. Stack the
        # pre_show response so both pre-bounce calls land on it, with the
        # post_show last so FakeBoxBackend's "peek-when-last" semantics pick
        # it up for the consistency check.
        return FakeBoxBackend({
            "validate": [
                {"ok": True, "errors": [], "exists": True},  # pre-bounce
                post_validate,                                # post-bounce
            ],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "show": [pre_show, pre_show, post_show],
            "applied-show": [None],
            "set-applied-hash": [{"ok": True}],
        })

    def test_consistent_state_updates_applied_hash(self):
        snap = {"version": 1, "apt_packages": [], "sysctl": {}, "mounts": []}
        backend = self._backend(
            pre_show=snap,
            post_show=snap,
            post_validate={"ok": True, "errors": [], "exists": True},
        )
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("set-applied-hash", [c[0] for c in backend.calls])

    def test_post_bounce_validation_failure_skips_applied_hash(self):
        # Someone wrote a malformed JSON over the file between pre-bounce
        # validate and post-bounce validate. Container is up (on the old
        # content), but the file is now invalid.
        snap = {"version": 1, "apt_packages": [], "sysctl": {}, "mounts": []}
        backend = self._backend(
            pre_show=snap,
            post_show=snap,
            post_validate={"ok": False, "errors": ["mounts[0]: missing 'host'"], "exists": True},
        )
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no longer validates", result.output)
        # applied-hash must NOT have been touched.
        self.assertNotIn("set-applied-hash", [c[0] for c in backend.calls])

    def test_post_bounce_show_drift_skips_applied_hash(self):
        # File was edited to a valid but different shape after pre-bounce read.
        pre = {"version": 1, "apt_packages": ["tcpdump"], "sysctl": {}, "mounts": []}
        post = {"version": 1, "apt_packages": ["tcpdump", "strace"], "sysctl": {}, "mounts": []}
        backend = self._backend(
            pre_show=pre,
            post_show=post,
            post_validate={"ok": True, "errors": [], "exists": True},
        )
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.apt_install",
                   return_value=__import__("cli.commands.box._host_ops", fromlist=["HostOpResult"]).HostOpResult(
                       ok=True, action="installed", message="ok")):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("was modified during apply", result.output)
        self.assertNotIn("set-applied-hash", [c[0] for c in backend.calls])


class DiffHelpers(unittest.TestCase):
    """Pure-function tests for the diff computation. Independent of click /
    SSH plumbing so failures pinpoint the helper, not the command wiring."""

    def test_empty_when_current_equals_applied(self):
        snap = {
            "mounts": [{"host": "/a", "container": "/a", "readonly": False}],
            "env": {"FOO": "1"},
            "sysctl": {"net.ipv4.ip_forward": "1"},
            "pip_packages": ["click"],
            "apt_packages": ["tcpdump"],
            "cargo_packages": [],
            "volumes": [],
        }
        diff = box_config_cli._compute_diff(snap, snap)
        self.assertTrue(box_config_cli._diff_is_empty(diff))

    def test_no_snapshot_treats_everything_as_added(self):
        current = {
            "mounts": [{"host": "/a", "container": "/a", "readonly": False}],
            "apt_packages": ["tcpdump"],
            "sysctl": {"k": "v"},
        }
        diff = box_config_cli._compute_diff(current, None)
        self.assertFalse(box_config_cli._diff_is_empty(diff))
        self.assertEqual(len(diff["mounts"]["added"]), 1)
        self.assertEqual(diff["apt_packages"]["added"], ["tcpdump"])
        self.assertEqual(diff["sysctl"]["added"], {"k": "v"})

    def test_mount_upsert_is_changed_not_paired_add_remove(self):
        applied = {"mounts": [{"host": "/old", "container": "/c", "readonly": False}]}
        current = {"mounts": [{"host": "/new", "container": "/c", "readonly": False}]}
        diff = box_config_cli._compute_diff(current, applied)
        self.assertEqual(diff["mounts"]["added"], [])
        self.assertEqual(diff["mounts"]["removed"], [])
        self.assertEqual(len(diff["mounts"]["changed"]), 1)
        change = diff["mounts"]["changed"][0]
        self.assertEqual(change["from"]["host"], "/old")
        self.assertEqual(change["to"]["host"], "/new")

    def test_sysctl_changed_reports_from_and_to(self):
        applied = {"sysctl": {"net.ipv4.ip_forward": "0", "kernel.shmmax": "1"}}
        current = {"sysctl": {"net.ipv4.ip_forward": "1", "kernel.shmmax": "1"}}
        diff = box_config_cli._compute_diff(current, applied)
        self.assertEqual(
            diff["sysctl"]["changed"],
            {"net.ipv4.ip_forward": {"from": "0", "to": "1"}},
        )
        self.assertEqual(diff["sysctl"]["added"], {})
        self.assertEqual(diff["sysctl"]["removed"], {})

    def test_pip_uses_set_semantics_with_sorted_output(self):
        applied = {"pip_packages": ["a", "b", "c"]}
        current = {"pip_packages": ["b", "d", "a"]}
        diff = box_config_cli._compute_diff(current, applied)
        self.assertEqual(diff["pip_packages"]["added"], ["d"])
        self.assertEqual(diff["pip_packages"]["removed"], ["c"])


class DiffCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_diff_prints_no_pending_when_clean(self):
        snap = {"version": 1, "apt_packages": ["tcpdump"]}
        backend = FakeBoxBackend({"show": [snap], "applied-show": [snap]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["diff", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("No pending changes", result.output)

    def test_diff_shows_pending_apt_change(self):
        current = {"version": 1, "apt_packages": ["tcpdump", "strace"]}
        applied = {"version": 1, "apt_packages": ["tcpdump"]}
        backend = FakeBoxBackend({"show": [current], "applied-show": [applied]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["diff", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Apt packages", result.output)
        self.assertIn("+ strace", result.output)
        self.assertNotIn("tcpdump", result.output.split("Apt packages")[1])

    def test_diff_json_round_trips(self):
        current = {"version": 1, "sysctl": {"k": "1"}}
        applied = {"version": 1, "sysctl": {}}
        backend = FakeBoxBackend({"show": [current], "applied-show": [applied]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["diff", "--box", "HYP-3", "--json"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["sysctl"]["added"], {"k": "1"})

    def test_diff_first_apply_flags_no_snapshot(self):
        current = {"version": 1, "apt_packages": ["tcpdump"]}
        backend = FakeBoxBackend({"show": [current], "applied-show": [None]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["diff", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("No applied snapshot", result.output)
        self.assertIn("+ tcpdump", result.output)


class ApplyDryRun(unittest.TestCase):
    """--dry-run takes no destructive action: no preflight chown, no apt,
    no sysctl, no bounce, no set-applied-hash. Only the read-only shim calls
    fire."""

    def setUp(self):
        self.runner = CliRunner()

    def _backend(self, *, current, applied, cur_hash="aaa", applied_hash="bbb"):
        return FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": cur_hash}],
            "applied-hash": [{"hash": applied_hash}],
            "show": [current],
            "applied-show": [applied],
        })

    def test_dry_run_skips_all_writes(self):
        current = {"version": 1, "apt_packages": ["tcpdump"], "sysctl": {}, "mounts": []}
        applied = {"version": 1, "apt_packages": [], "sysctl": {}, "mounts": []}
        backend = self._backend(current=current, applied=applied)
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container") as bounce_mock, \
             patch.object(box_config_cli, "_preflight_mounts") as prep_mock, \
             patch("cli.commands.box.config.apt_install") as apt_mock, \
             patch("cli.commands.box.config.sysctl_apply") as sysctl_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes", "--dry-run"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Dry run", result.output)
        self.assertIn("+ tcpdump", result.output)
        bounce_mock.assert_not_called()
        prep_mock.assert_not_called()
        apt_mock.assert_not_called()
        sysctl_mock.assert_not_called()
        # set-applied-hash must NOT be called: dry-run never claims success.
        verbs = [c[0] for c in backend.calls]
        self.assertNotIn("set-applied-hash", verbs)

    def test_dry_run_reports_clean_when_unchanged(self):
        snap = {"version": 1, "apt_packages": [], "sysctl": {}, "mounts": []}
        backend = self._backend(current=snap, applied=snap, cur_hash="x", applied_hash="x")
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container") as bounce_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "HYP-3", "--yes", "--dry-run"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("would be a no-op", result.output)
        bounce_mock.assert_not_called()


class AuditCommand(unittest.TestCase):
    """Host CLI surface for the audit-tail shim verb. Just renders entries —
    no audit logic on the host side."""

    def setUp(self):
        self.runner = CliRunner()

    def test_renders_entries_in_order(self):
        entries = [
            {"ts": "2026-05-11T12:00:00Z", "verb": "mount-add",
             "args": {"host": "/a", "container": "/a", "readonly": False}},
            {"ts": "2026-05-11T12:01:00Z", "verb": "pip-add",
             "args": {"added": ["click"]}},
        ]
        backend = FakeBoxBackend({"audit-tail": [{"entries": entries}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["audit", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        # Order preserved, both verbs visible.
        self.assertLess(result.output.index("mount-add"), result.output.index("pip-add"))
        self.assertIn("/a", result.output)
        self.assertIn("click", result.output)

    def test_empty_log_says_so(self):
        backend = FakeBoxBackend({"audit-tail": [{"entries": []}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["audit", "--box", "HYP-3"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("No audit entries", result.output)

    def test_tail_n_is_passed_to_shim(self):
        backend = FakeBoxBackend({"audit-tail": [{"entries": []}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            self.runner.invoke(
                box_config_cli.box_config,
                ["audit", "--tail", "5", "--box", "HYP-3"],
            )
        # The shim got "audit-tail" + "5"
        self.assertEqual(backend.calls, [("audit-tail", ("5",))])

    def test_json_flag_emits_array(self):
        entries = [{"ts": "2026-05-11T12:00:00Z", "verb": "init", "args": {}}]
        backend = FakeBoxBackend({"audit-tail": [{"entries": entries}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["audit", "--box", "HYP-3", "--json"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload, entries)


if __name__ == "__main__":
    unittest.main()
