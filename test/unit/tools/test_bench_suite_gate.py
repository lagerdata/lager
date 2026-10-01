# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`tools/bench_suite_gate.sh` must see skips, not only failures.

The gate ratchets each bench suite's failure count against a baseline. A check
that SKIPS where it used to pass never reaches that count, so a run reads "all
71 checks pass" with a whole section unexercised. uart.sh's device round-trip
went from 6/6 passing to 6/6 skipped between two nightlies, and every nightly
after stayed green.

These tests drive the real script on summaries shaped like harness.sh's:

    TOTAL  <total> <passed> <failed>              no skips
    TOTAL  <total> <passed> <failed> <excluded>   one or more skips
"""

import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / "tools" / "bench_suite_gate.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _gate(tmp_path, summary, *args):
    log = tmp_path / "suite.log"
    # Colour codes around the numbers, as the real suites print them.
    log.write_text("Section  Description  Total Passed Failed\n"
                   f"\x1b[1m{summary}\x1b[0m\n")
    return subprocess.run(["bash", str(GATE), "harness", "uart.sh", *args[:1], str(log), *args[1:]],
                          capture_output=True, text=True)


# Shapes taken from real runs: 09-24 (peer answered) and 09-26 (peer silent).
PASSING = "TOTAL                    71     71      0"
SKIPPING = "TOTAL                    71     65      0        6"


class TestWithoutASkipBudget:
    """Four arguments: the old contract, unchanged."""

    def test_a_clean_run_passes(self, tmp_path):
        assert _gate(tmp_path, PASSING, "0").returncode == 0

    def test_skips_alone_still_pass(self, tmp_path):
        res = _gate(tmp_path, SKIPPING, "0")
        assert res.returncode == 0, res.stdout

    def test_but_the_notice_says_how_many_were_skipped(self, tmp_path):
        res = _gate(tmp_path, SKIPPING, "0")
        assert "(6 skipped)" in res.stdout, (
            "'all 71 checks pass' with six skipped is how the section went "
            "unnoticed; the notice must name the skips")
        assert "all 71 checks pass" not in res.stdout

    def test_failures_over_baseline_still_fail(self, tmp_path):
        assert _gate(tmp_path, "TOTAL  71  70  1", "0").returncode == 1


class TestWithASkipBudget:
    def test_the_silent_peer_run_now_fails(self, tmp_path):
        res = _gate(tmp_path, SKIPPING, "0", "0")
        assert res.returncode == 1, res.stdout
        assert "6 checks skipped" in res.stdout

    def test_no_skip_column_means_zero_skips(self, tmp_path):
        """harness.sh omits the column when nothing was skipped."""
        assert _gate(tmp_path, PASSING, "0", "0").returncode == 0

    def test_skips_within_budget_pass(self, tmp_path):
        assert _gate(tmp_path, SKIPPING, "0", "6").returncode == 0

    def test_a_non_numeric_budget_is_an_error(self, tmp_path):
        res = _gate(tmp_path, PASSING, "0", "six")
        assert res.returncode == 1 and "max-skips" in res.stdout

    def test_the_budget_is_refused_for_the_deployment_format(self, tmp_path):
        log = tmp_path / "suite.log"
        log.write_text("Total Tests:   43\nFailed:        0\n")
        res = subprocess.run(["bash", str(GATE), "deployment", "deployment.sh", "0",
                              str(log), "0"], capture_output=True, text=True)
        assert res.returncode == 1 and "harness format only" in res.stdout

    def test_a_missing_summary_still_fails_first(self, tmp_path):
        log = tmp_path / "suite.log"
        log.write_text("the suite died before printing a summary\n")
        res = subprocess.run(["bash", str(GATE), "harness", "uart.sh", "0", str(log), "0"],
                             capture_output=True, text=True)
        assert res.returncode == 1 and "no test summary" in res.stdout
