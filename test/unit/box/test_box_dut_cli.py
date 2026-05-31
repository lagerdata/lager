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

    def __init__(self, read_body=""):
        self.read_body = read_body
        self.written = None
        self.calls = []

    def __call__(self, box_ip, cmd, *, stdin=None, timeout=60):
        self.calls.append((cmd, stdin))
        if stdin is not None:
            self.written = stdin
            return 0, "", ""
        # read path (cat ... || true)
        return 0, self.read_body, ""


def _patch(read_body=""):
    fake = FakeSsh(read_body=read_body)
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


if __name__ == "__main__":
    unittest.main()
