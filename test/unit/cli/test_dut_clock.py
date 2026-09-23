# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""``lager dut add-doc`` and ``edit`` stamp ``dut_updated_at`` on bench.json.

A control plane that keeps its own copy of the DUT context compares clocks
before it overwrites either side; a CLI write that left no clock would read as
older than any control-plane copy and be silently replaced on the next probe.
"""

import importlib
import re
from unittest.mock import patch

from click.testing import CliRunner

dut_mod = importlib.import_module("cli.commands.box.dut")

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _run_add_doc(existing):
    written = {}

    def fake_write(box_ip, payload):
        written.update(payload)
        return True

    runner = CliRunner()
    with patch.object(dut_mod, "_resolve_box", return_value="10.0.0.5"), \
         patch.object(dut_mod, "_read_bench_json", return_value=existing), \
         patch.object(dut_mod, "_write_bench_json", side_effect=fake_write):
        result = runner.invoke(dut_mod.box_dut, [
            "add-doc", "--kind", "datasheet", "--title", "MCU ref", "--url", "https://x/ref.pdf",
        ])
    return result, written


class TestDutClock:
    def test_add_doc_stamps_the_clock(self):
        result, written = _run_add_doc({"dut_slots": [{"name": "main"}]})
        assert result.exit_code == 0, result.output
        assert _ISO_Z.match(written["dut_updated_at"])
        assert written["dut_slots"][0]["datasheet_refs"][0]["title"] == "MCU ref"

    def test_add_doc_on_an_empty_bench_still_stamps(self):
        result, written = _run_add_doc({})
        assert result.exit_code == 0, result.output
        assert _ISO_Z.match(written["dut_updated_at"])

    def test_the_stamp_helper_writes_utc_seconds(self):
        payload = {}
        dut_mod._stamp_dut_clock(payload)
        assert _ISO_Z.match(payload["dut_updated_at"])
