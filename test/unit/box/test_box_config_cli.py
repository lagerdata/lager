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
                ["mount", "add", "/Hyphen", "/Hyphen", "--box", "test-box"],
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
                ["mount", "add", "/Hyphen", "/Hyphen", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Added mount /Hyphen -> /Hyphen", result.output)
        self.assertIn("mount-add", [c[0] for c in backend.calls])

    def test_mount_add_rerun_exits_zero_both_times(self):
        # run.sh is re-run after partial failures; the second identical
        # mount add must succeed (box-side upserts by container path).
        backend = FakeBoxBackend({"mount-add": [{"ok": True}, {"ok": True}]})
        args = ["mount", "add", "/Hyphen", "/Hyphen", "--readonly", "--box", "test-box"]
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch("cli.commands.box.config.ensure_host_path_owned",
                   return_value=self._make_prep(ok=True, action="ok_readonly", message="exists")):
            r1 = self.runner.invoke(box_config_cli.box_config, args)
            r2 = self.runner.invoke(box_config_cli.box_config, args)
        self.assertEqual(r1.exit_code, 0, msg=r1.output)
        self.assertEqual(r2.exit_code, 0, msg=r2.output)
        self.assertEqual([c[0] for c in backend.calls].count("mount-add"), 2)

    def test_no_auto_prep_skips_prep_and_persists_directly(self):
        backend = FakeBoxBackend({"mount-add": [{"ok": True}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch("cli.commands.box.config.ensure_host_path_owned") as prep_call:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["mount", "add", "/Hyphen", "/Hyphen", "--no-auto-prep", "--box", "test-box"],
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
        # `show` is consulted twice during apply: once by the apt/sysctl
        # host-side helpers and once by _preflight_mounts (which now runs
        # after them). The single registered response is reused for both.
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
                ["apply", "--box", "test-box", "--yes"],
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
                ["apply", "--box", "test-box", "--yes"],
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
    """Rollback uses direct SSH file ops (not the shim) because the
    container is dead by the time rollback fires — start_box.sh stopped
    and removed it before the failed `docker run`."""

    def setUp(self):
        self.runner = CliRunner()

    def _backend(self):
        return FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "show": [{"version": 1, "mounts": []}],
            "applied-show": [None],
        })

    def _ssh_runner(self, *, snapshot_exists, cp_succeeds=True):
        """Fake SSH that responds to the two commands rollback issues:
        `test -f .../applied.json` and `sudo -n cp .../applied.json
        .../box_config.json`. sudo is required because the destination is
        owned by www-data (uid 33), not the lagerdata SSH user."""
        def runner(box_ip, cmd, *, stdin=None, timeout=60):
            if cmd.startswith("test -f"):
                return (0 if snapshot_exists else 1), "", ""
            if cmd.startswith("sudo -n cp "):
                return (0 if cp_succeeds else 1), "", "" if cp_succeeds else "cp: permission denied"
            return 0, "", ""
        return runner

    def test_rollback_succeeds_when_snapshot_exists(self):
        backend = self._backend()
        # First bounce: fail. Second bounce (rollback): succeed.
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "default_ssh_runner",
                          side_effect=self._ssh_runner(snapshot_exists=True)), \
             patch.object(box_config_cli, "_bounce_container",
                          side_effect=[False, True]) as bounce_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Rolled back", result.output)
        self.assertEqual(bounce_mock.call_count, 2)
        # Critical: applied_hash must NOT have been updated to the (rejected)
        # new config's hash. The shim should NOT have been called for restore.
        verbs = [c[0] for c in backend.calls]
        self.assertNotIn("set-applied-hash", verbs)
        self.assertNotIn("restore-applied", verbs)

    def test_no_snapshot_no_rollback(self):
        backend = self._backend()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "default_ssh_runner",
                          side_effect=self._ssh_runner(snapshot_exists=False)), \
             patch.object(box_config_cli, "_bounce_container", return_value=False) as bounce_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("rollback was not possible", result.output)
        # We try the snapshot test once; the initial bounce already fired.
        self.assertEqual(bounce_mock.call_count, 1)
        verbs = [c[0] for c in backend.calls]
        self.assertNotIn("set-applied-hash", verbs)

    def test_rollback_bounce_also_fails(self):
        backend = self._backend()
        # Snapshot exists, file copy succeeds, but the rollback bounce also fails.
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "default_ssh_runner",
                          side_effect=self._ssh_runner(snapshot_exists=True)), \
             patch.object(box_config_cli, "_bounce_container", return_value=False) as bounce_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes"],
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
                ["apt", "add", "Bad Name", "--box", "test-box"],
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
                ["apt", "add", "tcpdump", "--box", "test-box"],
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
                ["apt", "remove", "tcpdump", "--box", "test-box"],
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
                ["apt", "list", "--box", "test-box"],
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
                ["sysctl", "set", "no_equals_here", "--box", "test-box"],
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
                ["sysctl", "set", "bad-key=1", "--box", "test-box"],
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
                ["sysctl", "set", "net.ipv4.ip_forward=1", "--box", "test-box"],
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
                ["sysctl", "unset", "net.ipv4.ip_forward", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Removed 1 sysctl key", result.output)


class EnvCli(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_set_requires_key_value(self):
        backend = FakeBoxBackend()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["env", "set", "no_equals_here", "--box", "test-box"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("expected KEY=VALUE", result.output)
        self.assertEqual(backend.calls, [])

    def test_set_round_trip_payload_shape(self):
        backend = FakeBoxBackend({
            "env-set": [{"ok": True, "set": ["LAGER_DEBUG", "LOG_LEVEL"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["env", "set", "LAGER_DEBUG=1", "LOG_LEVEL=info", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Set 2 env var", result.output)
        verb, args = backend.calls[-1]
        self.assertEqual(verb, "env-set")
        sent = json.loads(args[0])
        self.assertEqual(sent, {"entries": {"LAGER_DEBUG": "1", "LOG_LEVEL": "info"}})

    def test_set_value_with_equals_keeps_value_intact(self):
        # KEY=VALUE input split must use rsplit-style "first = wins" so values
        # containing `=` (URL params, query strings, base64 padding) pass
        # through to the shim unmangled.
        backend = FakeBoxBackend({"env-set": [{"ok": True, "set": ["DB_URL"]}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["env", "set", "DB_URL=postgres://u:p@h/db?ssl=true", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        sent = json.loads(backend.calls[-1][1][0])
        self.assertEqual(sent["entries"]["DB_URL"], "postgres://u:p@h/db?ssl=true")

    def test_set_rejects_path_via_shim(self):
        # Format validation is shim-side; the host CLI's job is to surface
        # the error message via _print_errors.
        backend = FakeBoxBackend({
            "env-set": [{"ok": False, "errors": ["'PATH': env key 'PATH' is not allowed; use 'PATH_PREPEND' to extend PATH inside the container"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["env", "set", "PATH=/usr/bin", "--box", "test-box"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Failed to set env vars", result.output)
        self.assertIn("PATH_PREPEND", result.output)

    def test_unset_round_trips(self):
        backend = FakeBoxBackend({
            "env-unset": [{"ok": True, "removed": ["LAGER_DEBUG"]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["env", "unset", "LAGER_DEBUG", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Removed 1 env var", result.output)

    def test_list_renders_entries(self):
        backend = FakeBoxBackend({
            "show": [{"version": 1, "env": {"LAGER_DEBUG": "1", "LOG_LEVEL": "info"}}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["env", "list", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("LAGER_DEBUG=1", result.output)
        self.assertIn("LOG_LEVEL=info", result.output)


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
                ["cargo", "add", "Bad-Crate", "--box", "test-box"],
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
                ["cargo", "add", "defmt-print@0.3.13", "--box", "test-box"],
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
                ["npm", "add", "Express", "--box", "test-box"],
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
                ["npm", "add", "@types/node@20.0.0", "--box", "test-box"],
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
                ["npm", "remove", "lodash", "--box", "test-box"],
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
                ["npm", "list", "--box", "test-box"],
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
                ["apply", "--box", "test-box", "--yes"],
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
                ["apply", "--box", "test-box", "--yes"],
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
                ["apply", "--box", "test-box", "--yes"],
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
                ["apply", "--box", "test-box", "--yes"],
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
                ["apply", "--box", "test-box", "--yes"],
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
                ["apply", "--box", "test-box", "--yes"],
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
                ["apply", "--box", "test-box", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("was modified during apply", result.output)
        self.assertNotIn("set-applied-hash", [c[0] for c in backend.calls])


class AuditFilters(unittest.TestCase):
    """Host-side `--since` / `--verb` filters on the audit log."""

    def setUp(self):
        self.runner = CliRunner()

    def _backend(self, entries):
        return FakeBoxBackend({"audit-tail": [{"entries": entries}]})

    def _entries(self):
        # Mix of timestamps and verbs so each filter has meaningful data.
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        def iso(dt):
            return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        return [
            {"ts": iso(now - datetime.timedelta(minutes=5)),  "verb": "apt-add",       "args": {"added": ["jq"]}},
            {"ts": iso(now - datetime.timedelta(hours=2)),    "verb": "pip-add",       "args": {"added": ["requests"]}},
            {"ts": iso(now - datetime.timedelta(days=3)),     "verb": "apt-add",       "args": {"added": ["strace"]}},
            {"ts": iso(now - datetime.timedelta(days=8)),     "verb": "set-applied-hash", "args": {"hash": "x"}},
        ]

    def test_since_filters_by_recency(self):
        entries = self._entries()
        backend = self._backend(entries)
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["audit", "--box", "test-box", "--since", "1h"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        # Only the 5-min-ago apt-add should pass the 1h cutoff.
        self.assertIn("jq", result.output)
        self.assertNotIn("requests", result.output)
        self.assertNotIn("strace", result.output)
        self.assertNotIn("hash", result.output)

    def test_verb_filters_to_exact_match(self):
        entries = self._entries()
        backend = self._backend(entries)
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["audit", "--box", "test-box", "--verb", "apt-add"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        # Both apt-add entries appear; pip-add and set-applied-hash filtered out.
        self.assertIn("jq", result.output)
        self.assertIn("strace", result.output)
        self.assertNotIn("requests", result.output)
        self.assertNotIn("set-applied-hash", result.output)

    def test_filters_compose(self):
        entries = self._entries()
        backend = self._backend(entries)
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["audit", "--box", "test-box", "--since", "1h", "--verb", "apt-add"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        # Intersection: only entries that are BOTH recent AND apt-add → just jq.
        self.assertIn("jq", result.output)
        self.assertNotIn("strace", result.output)

    def test_no_matches_says_so(self):
        entries = self._entries()
        backend = self._backend(entries)
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["audit", "--box", "test-box", "--verb", "never-existed"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("No matching audit entries", result.output)

    def test_since_format_validation(self):
        backend = FakeBoxBackend({"audit-tail": [{"entries": []}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["audit", "--box", "test-box", "--since", "1 fortnight"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("unsupported --since format", result.output)


class ApplyPreConfirmDiff(unittest.TestCase):
    """`apply` without `--yes` shows the diff before its confirm prompt
    so the operator sees exactly what's about to change."""

    def setUp(self):
        self.runner = CliRunner()

    def test_diff_printed_before_confirm(self):
        # Backend: pre-bounce validate ok, hash differs from applied-hash
        # (mutations pending), and show/applied-show reveal one apt change.
        backend = FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "show": [
                # current_show; _preflight_mounts never fires because the
                # test declines at the confirm prompt, which now precedes it.
                {"version": 1, "apt_packages": ["strace"]},
            ],
            "applied-show": [{"version": 1, "apt_packages": []}],
        })
        # Decline at the confirm prompt — we just want to see the diff appear.
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container") as bounce_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box"],
                input="n\n",
            )
        # Exit non-zero because we declined.
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Pending changes:", result.output)
        self.assertIn("+ strace", result.output)
        # The confirm prompt itself is present.
        self.assertIn("Apply box config", result.output)
        # We declined, so bounce never fired.
        bounce_mock.assert_not_called()


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
                ["diff", "--box", "test-box"],
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
                ["diff", "--box", "test-box"],
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
                ["diff", "--box", "test-box", "--json"],
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
                ["diff", "--box", "test-box"],
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
                ["apply", "--box", "test-box", "--yes", "--dry-run"],
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
                ["apply", "--box", "test-box", "--yes", "--dry-run"],
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
                ["audit", "--box", "test-box"],
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
                ["audit", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("No audit entries", result.output)

    def test_tail_n_is_passed_to_shim(self):
        backend = FakeBoxBackend({"audit-tail": [{"entries": []}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            self.runner.invoke(
                box_config_cli.box_config,
                ["audit", "--tail", "5", "--box", "test-box"],
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
                ["audit", "--box", "test-box", "--json"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload, entries)


class StatusCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_clean_state(self):
        show = {"version": 1, "apt_packages": ["tcpdump"]}
        backend = FakeBoxBackend({
            "show": [show],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "aaa"}],
            "audit-tail": [{"entries": [
                {"ts": "2026-05-11T12:00:00Z", "verb": "apt-add", "args": {"added": ["tcpdump"]}},
            ]}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["status", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("clean", result.output)
        self.assertIn("1 apt", result.output)
        self.assertIn("apt-add", result.output)

    def test_drift_state(self):
        backend = FakeBoxBackend({
            "show": [{"version": 1, "apt_packages": []}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "audit-tail": [{"entries": []}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["status", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("DRIFT", result.output)
        self.assertIn("diff --box", result.output)

    def test_no_config(self):
        backend = FakeBoxBackend({"show": [None]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["status", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("no box_config.json", result.output)

    def test_json_round_trips_single(self):
        backend = FakeBoxBackend({
            "show": [{"version": 1, "apt_packages": ["tcpdump"]}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "aaa"}],
            "audit-tail": [{"entries": []}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["status", "--box", "test-box", "--json"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["clean"])
        self.assertEqual(payload["counts"]["apt_packages"], 1)


class ExportImportCommands(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_export_writes_json_file(self):
        import os
        import tempfile
        payload = {"version": 1, "apt_packages": ["tcpdump"], "mounts": []}
        backend = FakeBoxBackend({"show": [payload]})
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out.json")
            with _patch_resolve(), \
                 patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
                result = self.runner.invoke(
                    box_config_cli.box_config,
                    ["export", out, "--box", "test-box"],
                )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            with open(out) as f:
                self.assertEqual(json.load(f), payload)

    def test_export_no_config_exits_nonzero(self):
        import os
        import tempfile
        backend = FakeBoxBackend({"show": [None]})
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out.json")
            with _patch_resolve(), \
                 patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
                result = self.runner.invoke(
                    box_config_cli.box_config,
                    ["export", out, "--box", "test-box"],
                )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("No box_config.json", result.output)

    def test_import_round_trips(self):
        import os
        import tempfile
        payload = {"version": 1, "apt_packages": ["tcpdump"]}
        backend = FakeBoxBackend({"set-raw": [{"ok": True, "hash": "abc"}]})
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.json")
            with open(src, "w") as f:
                json.dump(payload, f)
            with _patch_resolve(), \
                 patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
                result = self.runner.invoke(
                    box_config_cli.box_config,
                    ["import", src, "--box", "test-box", "--yes"],
                )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Imported", result.output)
            self.assertIn("set-raw", [c[0] for c in backend.calls])

    def test_import_rejects_invalid_json_before_ssh(self):
        import os
        import tempfile
        backend = FakeBoxBackend()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "bad.json")
            with open(src, "w") as f:
                f.write("{ this is not json")
            with _patch_resolve(), \
                 patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
                result = self.runner.invoke(
                    box_config_cli.box_config,
                    ["import", src, "--box", "test-box", "--yes"],
                )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Invalid JSON", result.output)
            self.assertEqual(backend.calls, [])

    def test_import_surfaces_shim_validation_errors(self):
        import os
        import tempfile
        payload = {"version": 1, "mounts": [{"host": "/h"}]}  # missing container
        backend = FakeBoxBackend({"set-raw": [{"ok": False, "errors": ["mounts[0]: missing required key 'container'."]}]})
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.json")
            with open(src, "w") as f:
                json.dump(payload, f)
            with _patch_resolve(), \
                 patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
                result = self.runner.invoke(
                    box_config_cli.box_config,
                    ["import", src, "--box", "test-box", "--yes"],
                )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("missing required key 'container'", result.output)


class CopyCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_copy_round_trips(self):
        payload = {"version": 1, "apt_packages": ["tcpdump"]}
        backend = FakeBoxBackend({
            "show": [payload],
            "set-raw": [{"ok": True, "hash": "abc"}],
        })
        # _resolve_box gets called twice; mock to return distinct values.
        with patch.object(box_config_cli, "_resolve_box", side_effect=["1.2.3.4", "5.6.7.8"]), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["copy", "--from", "test-box", "--to", "test-box-2", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Copied config", result.output)
        verbs_called = [c[0] for c in backend.calls]
        self.assertIn("show", verbs_called)
        self.assertIn("set-raw", verbs_called)

    def test_copy_refuses_same_box(self):
        backend = FakeBoxBackend()
        with patch.object(box_config_cli, "_resolve_box", return_value="1.2.3.4"), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["copy", "--from", "test-box", "--to", "test-box-alias", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("same box", result.output)
        self.assertEqual(backend.calls, [])


class EditCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_edit_saves_valid_changes(self):
        import os
        original = {"version": 1, "apt_packages": ["tcpdump"]}
        edited = {"version": 1, "apt_packages": ["tcpdump", "strace"]}
        backend = FakeBoxBackend({
            "show": [original],
            "set-raw": [{"ok": True, "hash": "new"}],
        })

        # Fake $EDITOR: rewrite the tempfile with the edited payload, return 0.
        def fake_editor(argv):
            tmp_path = argv[1]
            with open(tmp_path, "w") as f:
                json.dump(edited, f)
            return 0

        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch("subprocess.call", side_effect=fake_editor), \
             patch.dict(os.environ, {"EDITOR": "fake-editor"}):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["edit", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Saved on", result.output)
        verb, args = backend.calls[-1]
        self.assertEqual(verb, "set-raw")
        self.assertEqual(json.loads(args[0]), edited)

    def test_edit_reopens_on_validation_error(self):
        import os
        original = {"version": 1}
        bad = {"version": 1, "mounts": [{"host": "rel"}]}
        good = {"version": 1, "mounts": [{"host": "/a", "container": "/a"}]}
        backend = FakeBoxBackend({
            "show": [original],
            "set-raw": [
                {"ok": False, "errors": ["mounts[0].host must be an absolute path (got 'rel')."]},
                {"ok": True, "hash": "new"},
            ],
        })

        attempts = {"n": 0}

        def fake_editor(argv):
            tmp_path = argv[1]
            attempts["n"] += 1
            # First edit: write bad config. Second: write good.
            with open(tmp_path, "w") as f:
                json.dump(bad if attempts["n"] == 1 else good, f)
            return 0

        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch("subprocess.call", side_effect=fake_editor), \
             patch.dict(os.environ, {"EDITOR": "fake-editor"}):
            # Auto-answer "yes" to "Re-open editor?" prompt
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["edit", "--box", "test-box"],
                input="y\n",
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(attempts["n"], 2)
        self.assertIn("must be an absolute path", result.output)
        self.assertIn("Saved on", result.output)


class ShowTreeFormat(unittest.TestCase):
    """The human renderer draws a host/container tree with box-drawing
    characters. Pin a small instance so future tweaks don't silently
    break the structure (e.g., dropping the parent-line `│  ` continuations
    or losing the `(none)` leaf for `--all` empty sections)."""

    def setUp(self):
        self.runner = CliRunner()

    def _backend(self, payload):
        return FakeBoxBackend({
            "show": [payload],
            "hash": [{"hash": "x"}],
            "applied-hash": [{"hash": "x"}],
        })

    def test_sections_indented_under_group_headers(self):
        # Sections inside HOST/CONTAINER get a 2-space indent so membership
        # is visually clear; group headers themselves stay flush-left.
        payload = {"version": 1, "apt_packages": ["jq"]}
        backend = self._backend(payload)
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["show", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        lines = result.output.splitlines()
        # Group header flush left, section label indented.
        self.assertTrue(any(line == "HOST" for line in lines), msg=result.output)
        self.assertTrue(
            any(line.startswith("  ") and "Apt packages" in line for line in lines),
            msg=result.output,
        )
        # Entry lines inherit the same indent.
        self.assertTrue(
            any(line.startswith("  └── ") for line in lines)
            or any(line.startswith("  ├── ") for line in lines),
            msg=result.output,
        )

    def test_grouped_layout_with_section_branches(self):
        # HOST/CONTAINER group headers (bold, uppercase, underlined),
        # sections within each group with `├── /└── ` branches matching
        # nets's listing style. Need a multi-entry section so both branch
        # shapes appear.
        payload = {
            "version": 1,
            "apt_packages": ["tcpdump", "strace"],
            "pip_packages": ["requests"],
            "env": {"FOO": "1"},
        }
        backend = self._backend(payload)
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["show", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        out = result.output
        self.assertIn("Box config:", out)
        # No schema-version marker in the header (we shipped only v1 ever).
        self.assertNotIn("v1", out)
        # Group headers (caps + underline). Underline length matches the
        # header word so the visual width is consistent.
        self.assertIn("HOST", out)
        self.assertIn("CONTAINER", out)
        self.assertIn("─" * len("HOST"), out)
        self.assertIn("─" * len("CONTAINER"), out)
        # Section labels — plain (no scope-tag suffix).
        self.assertIn("Apt packages", out)
        self.assertIn("Pip packages", out)
        self.assertIn("Env", out)
        # Scope tags from the previous design must NOT appear anymore.
        self.assertNotIn("[host]", out)
        self.assertNotIn("[container]", out)
        # nets-style branches (4-char incl. trailing space).
        self.assertIn("├── ", out)
        self.assertIn("└── ", out)
        # No deep-tree `│  ` continuation prefix — single-level branches.
        self.assertNotIn("│  ", out)

    def test_empty_sections_render_with_none_leaf(self):
        # Discoverability over brevity: every section header appears even
        # when its content is empty, so operators can see what they could
        # add. Empty sections show a single `(none)` leaf branch.
        payload = {"version": 1, "apt_packages": ["tcpdump"]}
        backend = self._backend(payload)
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["show", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        out = result.output
        # Group headers are present even when one group is mostly empty.
        self.assertIn("HOST", out)
        self.assertIn("CONTAINER", out)
        # Populated section + every empty section's label visible.
        self.assertIn("Apt packages", out)
        self.assertIn("Sysctl", out)
        self.assertIn("Mounts", out)
        self.assertIn("Volumes", out)
        self.assertIn("Pip packages", out)
        self.assertIn("Cargo packages", out)
        self.assertIn("Npm packages", out)
        # Empty sections show a single `(none)` branch.
        self.assertIn("└── (none)", out)

    def test_status_marker_up_to_date(self):
        backend = FakeBoxBackend({
            "show": [{"version": 1, "apt_packages": ["tcpdump"]}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "aaa"}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["show", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("[Up To Date]", result.output)
        self.assertNotIn("Unapplied", result.output)

    def test_status_marker_unapplied(self):
        backend = FakeBoxBackend({
            "show": [{"version": 1, "apt_packages": ["tcpdump"]}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["show", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("[Unapplied Changes!]", result.output)
        self.assertNotIn("Up To Date", result.output)


class MultiBoxFanout(unittest.TestCase):
    """Comma-separated --box value fans out across boxes for show and apply."""

    def setUp(self):
        self.runner = CliRunner()

    def test_show_renders_each_box_with_header(self):
        # The new format uses per-box headers ("Box config: <label> (...)")
        # instead of "=== ip ===" separators. show_cmd also calls hash +
        # applied-hash to compute the clean/DRIFT marker.
        backend = FakeBoxBackend({
            "show": [
                {"version": 1, "apt_packages": ["tcpdump"]},
                {"version": 1, "apt_packages": ["strace"]},
            ],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "aaa"}],
        })
        with patch.object(box_config_cli, "_resolve_box", side_effect=["1.2.3.4", "5.6.7.8"]), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["show", "--box", "test-box,test-box-2"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        # Each box gets its own "Box config: ..." header line.
        self.assertEqual(result.output.count("Box config:"), 2)
        self.assertIn("1.2.3.4", result.output)
        self.assertIn("5.6.7.8", result.output)
        self.assertIn("tcpdump", result.output)
        self.assertIn("strace", result.output)

    def test_show_json_emits_per_box_map(self):
        backend = FakeBoxBackend({
            "show": [
                {"version": 1, "apt_packages": ["tcpdump"]},
                {"version": 1, "apt_packages": ["strace"]},
            ],
        })
        with patch.object(box_config_cli, "_resolve_box", side_effect=["1.2.3.4", "5.6.7.8"]), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["show", "--box", "test-box,test-box-2", "--json"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertIn("1.2.3.4", payload)
        self.assertIn("5.6.7.8", payload)

    def test_apply_continues_past_first_box_failure(self):
        # First box: validation fails. Second: applies cleanly. Final exit 1.
        backend = FakeBoxBackend({
            "validate": [
                {"ok": False, "errors": ["bad config"], "exists": True},
                {"ok": True, "errors": [], "exists": True},
            ],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "show": [{"version": 1, "mounts": []}],
            "applied-show": [None],
            "set-applied-hash": [{"ok": True}],
        })
        with patch.object(box_config_cli, "_resolve_box", side_effect=["1.2.3.4", "5.6.7.8"]), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box,test-box-2", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Refusing to apply", result.output)
        self.assertIn("Apply failed on 1/2", result.output)
        self.assertIn("1.2.3.4", result.output)


class RepairCommand(unittest.TestCase):
    """`lager box config repair` is the rollback path exposed as a verb,
    for recovering boxes that didn't trigger automatic rollback (e.g.,
    hand-edited bad JSON, container wedged on stale config)."""

    def setUp(self):
        self.runner = CliRunner()

    def _ssh_runner(self, *, snapshot_exists, cp_succeeds=True, cp_stderr=""):
        def runner(box_ip, cmd, *, stdin=None, timeout=60):
            if cmd.startswith("test -f"):
                return (0 if snapshot_exists else 1), "", ""
            if cmd.startswith("sudo -n cp "):
                return (0 if cp_succeeds else 1), "", cp_stderr
            return 0, "", ""
        return runner

    def test_repair_succeeds_end_to_end(self):
        with _patch_resolve(), \
             patch.object(box_config_cli, "default_ssh_runner",
                          side_effect=self._ssh_runner(snapshot_exists=True)), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["repair", "--box", "test-box", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Snapshot restored", result.output)
        self.assertIn("Repair complete", result.output)

    def test_repair_no_snapshot_aborts(self):
        with _patch_resolve(), \
             patch.object(box_config_cli, "default_ssh_runner",
                          side_effect=self._ssh_runner(snapshot_exists=False)):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["repair", "--box", "test-box", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No applied snapshot", result.output)

    def test_repair_cp_failure_surfaces_hint(self):
        # Stale sudoers (no cp clause) → sudo asks for password → sudo -n fails.
        with _patch_resolve(), \
             patch.object(box_config_cli, "default_ssh_runner",
                          side_effect=self._ssh_runner(
                              snapshot_exists=True, cp_succeeds=False,
                              cp_stderr="sudo: a password is required")):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["repair", "--box", "test-box", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Failed to restore snapshot", result.output)
        # The "run lager update" hint should appear for sudo-rule-missing case.
        self.assertIn("lager update", result.output)

    def test_repair_bounce_failure(self):
        with _patch_resolve(), \
             patch.object(box_config_cli, "default_ssh_runner",
                          side_effect=self._ssh_runner(snapshot_exists=True)), \
             patch.object(box_config_cli, "_bounce_container", return_value=False):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["repair", "--box", "test-box", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("container failed to start", result.output)


class UdevAddCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_add_sends_normalized_rule_payload(self):
        backend = FakeBoxBackend({"udev-add": [{"ok": True, "added": ["1209:0001"]}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["udev", "add", "0x1209:0001", "--usbtmc", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Added 1 udev rule", result.output)
        verb, args = next(c for c in backend.calls if c[0] == "udev-add")
        payload = json.loads(args[0])
        # 0x prefix stripped, lowercased; usbtmc flag carried through.
        self.assertEqual(
            payload["rules"],
            [{"vid": "1209", "pid": "0001", "mode": "0666", "usbtmc": True}],
        )

    def test_malformed_token_rejected_before_box_call(self):
        backend = FakeBoxBackend()  # udev-add intentionally NOT registered
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["udev", "add", "1209-0001", "--box", "test-box"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("expected VID:PID", result.output)
        self.assertEqual(backend.calls, [])


class ResetCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_reset_calls_reset_verb(self):
        backend = FakeBoxBackend({"reset": [{"ok": True}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["reset", "--box", "test-box", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Erased box config", result.output)
        self.assertIn("reset", [c[0] for c in backend.calls])

    def test_reset_apply_invokes_apply(self):
        backend = FakeBoxBackend({"reset": [{"ok": True}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_apply_one", return_value=True) as apply_one:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["reset", "--box", "test-box", "--yes", "--apply"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("reset", [c[0] for c in backend.calls])
        # --apply forces a bounce into the fresh empty config.
        apply_one.assert_called_once()
        self.assertTrue(apply_one.call_args.kwargs.get("force"))


class FieldRegistrySync(unittest.TestCase):
    """The box-side first-class field set (box/lager/box_config/config.py) and
    the CLI-side set (derived from _FIRST_CLASS_FIELDS_GROUPED in
    cli/commands/box/config.py) ship in separate trees and must stay in sync.
    A field added on one side only would silently break diff/show or extras
    round-tripping. This guard fails loudly on drift."""

    def test_box_and_cli_first_class_keys_match(self):
        import importlib.util
        import os
        box_cfg_path = os.path.abspath(os.path.join(
            os.path.dirname(__file__), '..', '..', '..',
            'box', 'lager', 'box_config', 'config.py',
        ))
        import sys
        spec = importlib.util.spec_from_file_location("box_cfg_sync_check", box_cfg_path)
        box_cfg = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = box_cfg  # dataclass annotation resolution needs this
        spec.loader.exec_module(box_cfg)
        self.assertEqual(
            set(box_cfg._FIRST_CLASS_KEYS),
            set(box_config_cli._FIRST_CLASS_KEYS),
            "box-side and CLI-side first-class field registries drifted; "
            "add the new field to both config.py files.",
        )


# ---------------------------------------------------------------------------
# apply: mount pre-flight — SSH-failure policy and ordering
# ---------------------------------------------------------------------------

def _prep(ok, action, host_path="/usr/bin/dfu-util", message="", manual_fix=None):
    from cli.commands.box._mount_prep import PrepResult
    return PrepResult(
        ok=ok, action=action, host_path=host_path,
        message=message, manual_fix=manual_fix,
    )


_SSH_FAILED_PREP = _prep(
    False, "ssh_failed",
    message=(
        "Could not SSH to juultest@10.101.9.207 to check /usr/bin/dfu-util: "
        "Permission denied (publickey,password)."
    ),
)


class ApplyPreflightSshWarn(unittest.TestCase):
    """An SSH transport failure during pre-flight warns and continues;
    verified-bad path states still abort before the bounce."""

    def setUp(self):
        self.runner = CliRunner()

    def _backend(self):
        current = {
            "version": 1,
            "apt_packages": [],
            "sysctl": {},
            "mounts": [{"host": "/usr/bin/dfu-util", "container": "/usr/local/bin/dfu-util", "readonly": True}],
        }
        return FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "show": [current],
            "applied-show": [None],
            "set-applied-hash": [{"ok": True}],
        })

    def test_ssh_failed_preflight_warns_and_continues(self):
        backend = self._backend()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True) as bounce_mock, \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.ensure_host_path_owned",
                   return_value=_SSH_FAILED_PREP):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Could not SSH to juultest@10.101.9.207", result.output)
        self.assertIn("continuing with apply", result.output)
        bounce_mock.assert_called_once()
        self.assertIn("set-applied-hash", [c[0] for c in backend.calls])

    def test_ssh_failed_checks_only_first_mount(self):
        # One transport failure means all remaining mounts would fail the
        # same way; don't pay an SSH round-trip per mount.
        backend = self._backend()
        current = backend.responses["show"][0]
        current["mounts"].append(
            {"host": "/usr/bin/lsusb", "container": "/usr/local/bin/lsusb", "readonly": True},
        )
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.ensure_host_path_owned",
                   return_value=_SSH_FAILED_PREP) as prep_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        prep_mock.assert_called_once()

    def test_refused_populated_still_aborts(self):
        backend = self._backend()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container") as bounce_mock, \
             patch("cli.commands.box.config.ensure_host_path_owned",
                   return_value=_prep(
                       False, "refused_populated",
                       message="/usr/bin/dfu-util is owned by 1000:1000 and contains files.",
                       manual_fix="sudo chown -R 33:33 /usr/bin/dfu-util",
                   )):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("failed pre-flight", result.output)
        bounce_mock.assert_not_called()
        self.assertNotIn("set-applied-hash", [c[0] for c in backend.calls])

    def test_sudo_failed_still_aborts(self):
        backend = self._backend()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container") as bounce_mock, \
             patch("cli.commands.box.config.ensure_host_path_owned",
                   return_value=_prep(
                       False, "sudo_failed",
                       message="passwordless sudo is not configured on the box.",
                       manual_fix="sudo mkdir -p /usr/bin/dfu-util",
                   )):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        bounce_mock.assert_not_called()


class ApplyPreflightOrdering(unittest.TestCase):
    """Pre-flight must run after apt provisioning (a mount's host path may be
    a file installed by an apt package in the same apply) and after the
    confirm prompt (no host mutation before the operator says yes)."""

    def setUp(self):
        self.runner = CliRunner()

    def _backend(self):
        current = {
            "version": 1,
            "apt_packages": ["dfu-util"],
            "sysctl": {},
            "mounts": [{"host": "/usr/bin/dfu-util", "container": "/usr/local/bin/dfu-util", "readonly": True}],
        }
        applied = {"version": 1, "apt_packages": [], "sysctl": {}, "mounts": []}
        return FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "show": [current],
            "applied-show": [applied],
            "set-applied-hash": [{"ok": True}],
        })

    def test_preflight_runs_after_apt_and_before_bounce(self):
        from cli.commands.box._host_ops import HostOpResult
        order = []
        apt_result = HostOpResult(ok=True, action="installed", message="Installed 1 apt package(s).")

        def fake_apt(*args, **kwargs):
            order.append("apt")
            return apt_result

        def fake_prep(*args, **kwargs):
            order.append("preflight")
            return _prep(True, "ok_readonly", message="/usr/bin/dfu-util exists.")

        def fake_bounce(*args, **kwargs):
            order.append("bounce")
            return True

        backend = self._backend()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", side_effect=fake_bounce), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.apt_install", side_effect=fake_apt), \
             patch("cli.commands.box.config.ensure_host_path_owned", side_effect=fake_prep):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(order, ["apt", "preflight", "bounce"])

    def test_preflight_not_run_when_confirm_declined(self):
        backend = self._backend()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container") as bounce_mock, \
             patch("cli.commands.box.config.ensure_host_path_owned") as prep_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box"],
                input="n\n",
            )
        self.assertNotEqual(result.exit_code, 0)
        prep_mock.assert_not_called()
        bounce_mock.assert_not_called()

    def test_skip_restart_skips_preflight(self):
        backend = self._backend()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch("cli.commands.box.config.ensure_host_path_owned") as prep_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes", "--skip-restart"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        prep_mock.assert_not_called()
        self.assertIn("set-applied-hash", [c[0] for c in backend.calls])


class ApplyFlagEdges(unittest.TestCase):
    """--force, --dry-run, and --no-auto-prep interplay with the pre-flight."""

    def setUp(self):
        self.runner = CliRunner()

    def _backend(self, cur_hash="aaa", applied_hash="bbb"):
        current = {
            "version": 1,
            "apt_packages": [],
            "sysctl": {},
            "mounts": [{"host": "/Hyphen", "container": "/Hyphen", "readonly": False}],
        }
        return FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": cur_hash}],
            "applied-hash": [{"hash": applied_hash}],
            "show": [current],
            "applied-show": [current],
            "set-applied-hash": [{"ok": True}],
        })

    def test_force_unchanged_hash_runs_preflight_before_bounce(self):
        order = []

        def fake_prep(*args, **kwargs):
            order.append("preflight")
            return _prep(True, "ok", message="/Hyphen already owned by 33:33.")

        def fake_bounce(*args, **kwargs):
            order.append("bounce")
            return True

        backend = self._backend(cur_hash="aaa", applied_hash="aaa")
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", side_effect=fake_bounce), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.ensure_host_path_owned", side_effect=fake_prep):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes", "--force"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(order, ["preflight", "bounce"])

    def test_dry_run_with_mounts_never_touches_ssh(self):
        backend = self._backend()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container") as bounce_mock, \
             patch("cli.commands.box.config.ensure_host_path_owned") as prep_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--dry-run"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        prep_mock.assert_not_called()
        bounce_mock.assert_not_called()

    def test_no_auto_prep_skips_preflight_but_bounces(self):
        backend = self._backend()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True) as bounce_mock, \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.ensure_host_path_owned") as prep_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes", "--no-auto-prep"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        prep_mock.assert_not_called()
        bounce_mock.assert_called_once()


class ApplyMultiBoxFanout(unittest.TestCase):
    """apply --box A,B: per-box isolation of warn-and-continue and failures."""

    def setUp(self):
        self.runner = CliRunner()

    def _backend(self):
        current = {
            "version": 1,
            "apt_packages": [],
            "sysctl": {},
            "mounts": [{"host": "/Hyphen", "container": "/Hyphen", "readonly": True}],
        }
        return FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "show": [current],
            "applied-show": [None],
            "set-applied-hash": [{"ok": True}],
        })

    def test_ssh_warn_on_one_box_does_not_fail_fanout(self):
        backend = self._backend()
        preps = iter([_SSH_FAILED_PREP, _prep(True, "ok_readonly", message="/Hyphen exists.")])
        with patch.object(box_config_cli, "_resolve_boxes", return_value=["1.1.1.1", "2.2.2.2"]), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.ensure_host_path_owned",
                   side_effect=lambda *a, **k: next(preps)):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "1.1.1.1,2.2.2.2", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("=== 1.1.1.1 ===", result.output)
        self.assertIn("=== 2.2.2.2 ===", result.output)
        self.assertIn("continuing with apply", result.output)

    def test_hard_failure_on_one_box_continues_and_aggregates(self):
        backend = self._backend()
        preps = iter([
            _prep(False, "refused_populated",
                  message="/Hyphen is owned by 1000:1000 and contains files.",
                  manual_fix="sudo chown -R 33:33 /Hyphen"),
            _prep(True, "ok_readonly", message="/Hyphen exists."),
        ])
        with patch.object(box_config_cli, "_resolve_boxes", return_value=["1.1.1.1", "2.2.2.2"]), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True) as bounce_mock, \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.ensure_host_path_owned",
                   side_effect=lambda *a, **k: next(preps)):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "1.1.1.1,2.2.2.2", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Apply failed on 1/2 box(es): 1.1.1.1", result.output)
        # Box 2 still got its bounce despite box 1 failing.
        bounce_mock.assert_called_once()


class ApplyPreflightMixedResults(unittest.TestCase):
    """Interactions between verified-bad and ssh_failed within one box."""

    def setUp(self):
        self.runner = CliRunner()

    def _backend_two_mounts(self):
        current = {
            "version": 1,
            "apt_packages": [],
            "sysctl": {},
            "mounts": [
                {"host": "/m1", "container": "/m1", "readonly": False},
                {"host": "/m2", "container": "/m2", "readonly": False},
            ],
        }
        return FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "show": [current],
            "applied-show": [None],
            "set-applied-hash": [{"ok": True}],
        })

    def test_real_failure_then_ssh_failed_aborts(self):
        backend = self._backend_two_mounts()
        preps = iter([
            _prep(False, "refused_populated", host_path="/m1",
                  message="/m1 is owned by 1000:1000 and contains files.",
                  manual_fix="sudo chown -R 33:33 /m1"),
            _prep(False, "ssh_failed", host_path="/m2",
                  message="Could not SSH to lagerdata@1.2.3.4 to check /m2: ..."),
        ])
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container") as bounce_mock, \
             patch("cli.commands.box.config.ensure_host_path_owned",
                   side_effect=lambda *a, **k: next(preps)):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("failed pre-flight", result.output)
        bounce_mock.assert_not_called()

    def test_non_string_host_skipped_without_crash(self):
        current = {
            "version": 1, "apt_packages": [], "sysctl": {},
            "mounts": [{"host": None, "container": "/x", "readonly": False}],
        }
        backend = FakeBoxBackend({
            "validate": [{"ok": True, "errors": [], "exists": True}],
            "hash": [{"hash": "aaa"}],
            "applied-hash": [{"hash": "bbb"}],
            "show": [current],
            "applied-show": [None],
            "set-applied-hash": [{"ok": True}],
        })
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=True), \
             patch.object(box_config_cli, "_wait_for_box_api", return_value=True), \
             patch("cli.commands.box.config.ensure_host_path_owned") as prep_mock:
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        prep_mock.assert_not_called()

    def test_bounce_failure_after_ssh_warn_engages_rollback(self):
        backend = self._backend_two_mounts()
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch.object(box_config_cli, "_bounce_container", return_value=False), \
             patch.object(box_config_cli, "_attempt_rollback", return_value=True) as rb_mock, \
             patch("cli.commands.box.config.ensure_host_path_owned",
                   return_value=_SSH_FAILED_PREP):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["apply", "--box", "test-box", "--yes"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("continuing with apply", result.output)
        rb_mock.assert_called_once()
        self.assertIn("Rolled back", result.output)


class MountAddSshFailed(unittest.TestCase):
    """`mount add` persists the mount when prep fails only because the box
    host was unreachable over SSH — apply re-checks before the restart."""

    def setUp(self):
        self.runner = CliRunner()

    def test_ssh_failed_prep_warns_and_persists(self):
        backend = FakeBoxBackend({"mount-add": [{"ok": True}]})
        with _patch_resolve(), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend), \
             patch("cli.commands.box.config.ensure_host_path_owned",
                   return_value=_SSH_FAILED_PREP):
            result = self.runner.invoke(
                box_config_cli.box_config,
                ["mount", "add", "/usr/bin/dfu-util", "/usr/local/bin/dfu-util",
                 "--readonly", "--box", "test-box"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("mount-add", [c[0] for c in backend.calls])
        self.assertIn("Could not SSH to juultest@10.101.9.207", result.output)
        self.assertIn("re-checks the host path", result.output)
        self.assertIn("Added mount /usr/bin/dfu-util -> /usr/local/bin/dfu-util (ro)", result.output)


if __name__ == "__main__":
    unittest.main()
