# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
CliRunner-based tests for cli/commands/box/dut.py.

Focus: the detached-list regression. On a box whose bench.json has no DUT
block yet, `_extract_dut_block` synthesizes a default `dut_slots` list that
was never attached to `payload`; `_primary_slot` then handed back a
`slot_idx` whose write-back path (`payload["dut_slots"][slot_idx]`) raised
KeyError. The SSH transport is mocked so each test is a pure CLI exercise --
no SSH, no filesystem on /etc/lager.
"""
import json
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from cli.commands.box import dut as box_dut_cli


class FakeSsh:
    """Stand-in for default_ssh_runner.

    Returns ``read_body`` for the bench.json read (the ``cat`` command) and
    captures the body piped in on write. The write path is the only one
    that pipes a stdin body, so that's the discriminator. Every call records
    (cmd, stdin) so tests can assert on the transport.
    """

    def __init__(self, read_body="", write_result=(0, "", "")):
        self.read_body = read_body
        self.write_result = write_result
        self.written = None
        self.calls = []

    def __call__(self, box_ip, cmd, *, stdin=None, timeout=60):
        self.calls.append((cmd, stdin))
        if stdin is not None:
            self.written = stdin
            return self.write_result
        # read path (cat ... || true)
        return 0, self.read_body, ""


def _patch(read_body="", write_result=(0, "", "")):
    fake = FakeSsh(read_body=read_body, write_result=write_result)
    return fake, patch.multiple(
        box_dut_cli,
        default_ssh_runner=fake,
        _resolve_box=lambda ctx, box: "1.2.3.4",
    )


# ---------------------------------------------------------------------------
# _primary_slot: the unit at the heart of the bug
# ---------------------------------------------------------------------------

class PrimarySlotAttachesList(unittest.TestCase):
    def test_empty_payload_attaches_dut_slots(self):
        """A bench.json with no DUT block must leave payload writable."""
        payload: dict = {}
        slot, slot_idx = box_dut_cli._primary_slot(payload)
        self.assertEqual(slot_idx, 0)
        # The synthesized list is now attached AND is the same object the
        # returned slot lives in, so the write-back path won't KeyError.
        self.assertIn("dut_slots", payload)
        self.assertIs(payload["dut_slots"][slot_idx], slot)

    def test_existing_active_slot_attached(self):
        payload = {"dut_slots": [{"name": "main", "active": True}]}
        slot, slot_idx = box_dut_cli._primary_slot(payload)
        self.assertEqual(slot_idx, 0)
        self.assertIs(payload["dut_slots"][slot_idx], slot)

    def test_dut_context_shortform_returns_none_index(self):
        payload = {"dut_context": {"name": "only"}}
        slot, slot_idx = box_dut_cli._primary_slot(payload)
        self.assertIsNone(slot_idx)
        self.assertIs(slot, payload["dut_context"])


# ---------------------------------------------------------------------------
# add-doc end-to-end via CliRunner
# ---------------------------------------------------------------------------

class AddDocCmd(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_add_doc_on_empty_bench_does_not_keyerror(self):
        """Regression: add-doc against a box with no DUT block must write."""
        fake, ctx = _patch(read_body="")
        with ctx:
            result = self.runner.invoke(
                box_dut_cli.box_dut,
                ["add-doc", "--box", "b", "--kind", "schematic",
                 "--title", "Main", "--repo-path", "docs/sch.pdf"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIsNotNone(fake.written, "bench.json was never written")
        written = json.loads(fake.written)
        slot = written["dut_slots"][0]
        self.assertEqual(slot["schematic_refs"][0]["title"], "Main")
        self.assertEqual(slot["schematic_refs"][0]["repo_path"], "docs/sch.pdf")

    def test_add_doc_appends_to_existing_active_slot(self):
        existing = json.dumps({
            "dut_slots": [
                {"name": "main", "active": True,
                 "datasheet_refs": [{"title": "Old", "kind": "datasheet"}]},
            ],
        })
        fake, ctx = _patch(read_body=existing)
        with ctx:
            result = self.runner.invoke(
                box_dut_cli.box_dut,
                ["add-doc", "--box", "b", "--kind", "datasheet",
                 "--title", "New", "--url", "https://example.com/rm.pdf"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        written = json.loads(fake.written)
        titles = [d["title"] for d in written["dut_slots"][0]["datasheet_refs"]]
        self.assertEqual(titles, ["Old", "New"])

    def test_add_doc_dut_context_shortform_roundtrips(self):
        existing = json.dumps({"dut_context": {"name": "only", "active": True}})
        fake, ctx = _patch(read_body=existing)
        with ctx:
            result = self.runner.invoke(
                box_dut_cli.box_dut,
                ["add-doc", "--box", "b", "--kind", "firmware",
                 "--title", "FW", "--repo-path", "build/app.elf"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        written = json.loads(fake.written)
        # Short-form preserved (no dut_slots key invented).
        self.assertNotIn("dut_slots", written)
        self.assertEqual(
            written["dut_context"]["firmware_refs"][0]["title"], "FW",
        )

    def test_add_doc_requires_url_or_repo_path(self):
        fake, ctx = _patch(read_body="")
        with ctx:
            result = self.runner.invoke(
                box_dut_cli.box_dut,
                ["add-doc", "--box", "b", "--title", "Main"],
            )
        self.assertEqual(result.exit_code, 1)
        self.assertIsNone(fake.written, "should not write when args invalid")

    def test_add_doc_write_failure_exits_nonzero(self):
        """A failed bench.json write must exit(1), not traceback."""
        fake, ctx = _patch(
            read_body="",
            write_result=(1, "", "sudo: a password is required\r\n"),
        )
        with ctx:
            result = self.runner.invoke(
                box_dut_cli.box_dut,
                ["add-doc", "--box", "b", "--kind", "schematic",
                 "--title", "Main", "--repo-path", "docs/sch.pdf"],
            )
        self.assertEqual(result.exit_code, 1, msg=result.output)
        self.assertNotIn("Traceback", result.output)


# ---------------------------------------------------------------------------
# _write_bench_json: the remote command string and failure surfacing
# ---------------------------------------------------------------------------

class WriteBenchJsonCommand(unittest.TestCase):
    """The write path must stage in /tmp (never inside www-data-owned
    /etc/lager) and fall back to the passwordless-sudo grant."""

    def _run(self, payload=None, write_result=(0, "", "")):
        fake = FakeSsh(write_result=write_result)
        with patch.object(box_dut_cli, "default_ssh_runner", fake):
            ok = box_dut_cli._write_bench_json("1.2.3.4", payload or {"k": "v"})
        return fake, ok

    def test_stages_to_tmp_not_etc_lager(self):
        fake, ok = self._run({"k": "v"})
        self.assertTrue(ok)
        cmd = fake.calls[-1][0]
        self.assertIn("/tmp/lager-bench.json.tmp", cmd)
        # The old bug staged the temp inside the target dir.
        self.assertNotIn("cat > /etc/lager", cmd)
        self.assertNotIn("bench.json.tmp.$$", cmd)
        # Body still travels over stdin, intact.
        self.assertEqual(json.loads(fake.written), {"k": "v"})

    def test_has_sudo_cp_and_chmod_fallback(self):
        cmd = self._run()[0].calls[-1][0]
        self.assertIn(
            "sudo -n /bin/cp /tmp/lager-bench.json.tmp /etc/lager/bench.json", cmd)
        self.assertIn("sudo -n /bin/chmod 644 /etc/lager/bench.json", cmd)

    def test_tries_unprivileged_mv_before_sudo(self):
        cmd = self._run()[0].calls[-1][0]
        mv_i = cmd.index("mv -f /tmp/lager-bench.json.tmp /etc/lager/bench.json")
        cp_i = cmd.index("sudo -n /bin/cp")
        self.assertLess(mv_i, cp_i, "unprivileged mv must be tried before sudo")

    def test_cleans_up_tmp(self):
        self.assertIn("rm -f /tmp/lager-bench.json.tmp", self._run()[0].calls[-1][0])

    def test_missing_grant_is_actionable_and_banner_stripped(self):
        stderr = (
            "Warning: Permanently added '1.2.3.4' (ED25519) to the list of known hosts.\r\n"
            "sudo: a password is required to run sudo\r\n"
        )
        with patch.object(box_dut_cli.click, "secho") as secho:
            _, ok = self._run(write_result=(1, "", stderr))
        self.assertFalse(ok)
        msg = secho.call_args[0][0]
        self.assertNotIn("Permanently added", msg)
        self.assertIn("lager update", msg)
        self.assertIn("NOPASSWD", msg)

    def test_plain_error_surfaced_without_bootstrap(self):
        stderr = (
            "Warning: Permanently added '1.2.3.4' (ED25519) to the list of known hosts.\r\n"
            "cp: cannot create regular file '/etc/lager/bench.json': Read-only file system\r\n"
        )
        with patch.object(box_dut_cli.click, "secho") as secho:
            _, ok = self._run(write_result=(1, "", stderr))
        self.assertFalse(ok)
        msg = secho.call_args[0][0]
        self.assertIn("Read-only file system", msg)
        self.assertNotIn("Permanently added", msg)
        self.assertNotIn("NOPASSWD", msg)


if __name__ == "__main__":
    unittest.main()
