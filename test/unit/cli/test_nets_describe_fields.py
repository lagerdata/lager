# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""``lager nets describe`` writes the two per-net fields the control plane
keeps, ``dut_connection`` and ``test_hints``, next to purpose, notes and
tags, and touches nothing else on the record."""

import importlib
from unittest.mock import patch

from click.testing import CliRunner

# The package re-exports the `nets` click group under the submodule's own
# name, so a dotted patch target resolves to the group on Python 3.10 (where
# mock walks attributes instead of importing the full path). Patch the module
# object, as the other nets tests do.
nets_mod = importlib.import_module("cli.commands.box.nets")


def _run(args, record):
    saved = {}

    def fake_save(ctx, box, target):
        saved.update(target)

    runner = CliRunner()
    with patch.object(nets_mod, "_resolve_box", return_value="10.0.0.5"), \
         patch.object(nets_mod, "_fetch_saved_nets", return_value=[record]), \
         patch.object(nets_mod, "_save_net_http", side_effect=fake_save):
        result = runner.invoke(nets_mod.nets, ["describe", *args])
    return result, saved


def _record():
    return {"name": "uart1", "role": "uart", "instrument": "SiLabs_CP210x",
            "tags": ["console"], "test_hints": ["old hint"], "jlink_script": "Zm9v"}


class TestDescribeFields:
    def test_sets_dut_connection_and_appends_a_hint(self):
        result, saved = _run(
            ["uart1", "--dut-connection", "J3 pin 4", "--test-hint", "hold nRST low"],
            _record(),
        )
        assert result.exit_code == 0, result.output
        assert saved["dut_connection"] == "J3 pin 4"
        assert saved["test_hints"] == ["old hint", "hold nRST low"]
        # Untouched fields survive: the write is a merge, not a replacement.
        assert saved["tags"] == ["console"]
        assert saved["jlink_script"] == "Zm9v"

    def test_clear_test_hints_then_add(self):
        result, saved = _run(
            ["uart1", "--clear-test-hints", "--test-hint", "fresh"], _record(),
        )
        assert result.exit_code == 0
        assert saved["test_hints"] == ["fresh"]

    def test_duplicate_hints_collapse(self):
        _, saved = _run(["uart1", "--test-hint", "old hint", "--test-hint", "old hint"], _record())
        assert saved["test_hints"] == ["old hint"]

    def test_nothing_given_names_the_new_options(self):
        result, saved = _run(["uart1"], _record())
        assert result.exit_code == 0
        assert saved == {}
        assert "--dut-connection" in result.output
        assert "--test-hint" in result.output
