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
import re
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

    def test_a_missing_summary_still_fails_first_for_deployment_too(self, tmp_path):
        log = tmp_path / "suite.log"
        log.write_text("deployment.sh died early\n")
        res = subprocess.run(["bash", str(GATE), "deployment", "deployment.sh", "0",
                              str(log), "0"], capture_output=True, text=True)
        assert res.returncode == 1 and "no test summary" in res.stdout

    def test_a_missing_summary_still_fails_first(self, tmp_path):
        log = tmp_path / "suite.log"
        log.write_text("the suite died before printing a summary\n")
        res = subprocess.run(["bash", str(GATE), "harness", "uart.sh", "0", str(log), "0"],
                             capture_output=True, text=True)
        assert res.returncode == 1 and "no test summary" in res.stdout


def _deployment(tmp_path, total, failed, skipped, *budget):
    log = tmp_path / "suite.log"
    # deployment.sh's own summary block, colour codes included.
    lines = [f"Total Tests:   \x1b[1m{total}\x1b[0m", f"Failed:        \x1b[31m{failed}\x1b[0m"]
    if skipped is not None:
        lines.append(f"Skipped:       \x1b[33m{skipped}\x1b[0m")
    log.write_text("\n".join(lines) + "\n")
    return subprocess.run(["bash", str(GATE), "deployment", "deployment.sh", "0",
                           str(log), *budget], capture_output=True, text=True)


class TestDeploymentFormatSkips:
    """deployment.sh counts for itself; its skips must be visible to the gate.

    It skips every box-contacting check when the box's login user is not the
    expected one, so a mis-registered box used to pass with most checks
    unexercised.
    """

    def test_skips_over_the_budget_fail(self, tmp_path):
        res = _deployment(tmp_path, 43, 0, 7, "0")
        assert res.returncode == 1, res.stdout
        assert "7 checks skipped" in res.stdout

    def test_zero_skips_pass_a_zero_budget(self, tmp_path):
        assert _deployment(tmp_path, 43, 0, 0, "0").returncode == 0

    def test_a_summary_without_a_skip_line_counts_as_zero(self, tmp_path):
        assert _deployment(tmp_path, 43, 0, None, "0").returncode == 0

    def test_skips_are_named_in_the_notice(self, tmp_path):
        res = _deployment(tmp_path, 43, 0, 2)
        assert res.returncode == 0 and "(2 skipped)" in res.stdout


class TestEveryHarnessSuiteHasASkipBudget:
    """A suite gated without a skip budget can skip silently.

    That is the hole the budget exists to close, so every gate call in a
    workflow must pass one, set in the same step's env to a non-negative
    integer. A newly wired suite that forgets it fails here. Calls must sit on
    one line: a backslash continuation would hide the call from this scan.
    """

    CALL = re.compile(r'bench_suite_gate\.sh\s+(?:harness|deployment)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(\S+))?')

    def _calls(self):
        import yaml
        for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in (doc.get("jobs") or {}).values():
                for step in (job.get("steps") or []) if isinstance(job, dict) else []:
                    for m in self.CALL.finditer(step.get("run") or ""):
                        yield path.name, step, m

    def test_the_scan_finds_the_known_suites(self):
        suites = {m.group(1) for _, _, m in self._calls()}
        assert {"uart.sh", "nets.sh", "generic.sh", "box_config.sh",
                "jlink_script.sh", "deployment.sh"} <= suites, suites

    def test_no_gate_call_is_split_across_lines(self):
        """The scan above reads one line; a continued call would escape it."""
        for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "bench_suite_gate.sh" in line and line.rstrip().endswith("\\"):
                    raise AssertionError(f"{path.name}:{n}: gate call continued onto the next line")

    def test_every_call_passes_a_budget_set_in_its_step(self):
        offenders = []
        for wf, step, m in self._calls():
            budget = m.group(4)
            var = (budget or "").strip('"').lstrip("$").strip("{}")
            value = str((step.get("env") or {}).get(var, ""))
            if not budget or not value.isdigit():
                offenders.append(f"{wf}: {m.group(1)} (budget arg {budget!r}, env {var}={value!r})")
        assert not offenders, ("harness gate calls without a skip budget set in "
                               "their step env:\n  " + "\n  ".join(offenders))
