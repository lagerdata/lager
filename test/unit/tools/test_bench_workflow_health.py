# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The watchdog must see every bench workflow, not only the nightly.

Everything `tools/bench_schedule_check.py` checked read nightly-bench.yml's run
history. Bench: Extended was switched to `disabled_manually` and missed at least
three Saturdays with nothing reporting it: a disabled workflow produces no run,
so no notify job fires, and the watchdog never looked at it.

These tests pin the two checks that close that, and the routing that keeps them
from looping: a problem about Extended goes to `problems-extended.txt`, which
the workflow files under Extended's own label. On `bench-alert` the nightly's
recovery would close a still-true Extended problem on the next green night,
and the next watchdog run would file it again -- the #568 loop.
"""

import importlib.util
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "tools" / "bench_schedule_check.py"

_spec = importlib.util.spec_from_file_location("bench_schedule_check", TOOL)
bsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsc)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

NIGHTLY = ".github/workflows/nightly-bench.yml"
INTEGRATION = ".github/workflows/integration-tests.yml"
EXTENDED = ".github/workflows/bench-extended.yml"


def _iso(when):
    return when.isoformat().replace("+00:00", "Z")


def sched(hours_ago, now=NOW):
    return {"databaseId": int(hours_ago * 100), "status": "completed",
            "event": "schedule", "createdAt": _iso(now - timedelta(hours=hours_ago))}


def wf(path, state="active"):
    return {"path": path, "state": state, "name": path.rsplit("/", 1)[-1]}


class TestWhichWorkflowsAreBench:
    def test_every_bench_workflow_in_the_tree_is_found(self):
        paths = bsc.bench_workflow_paths()
        for expected in (NIGHTLY, INTEGRATION, EXTENDED,
                         ".github/workflows/update-regression.yml",
                         ".github/workflows/bench-watchdog.yml"):
            assert expected in paths, (
                f"{expected} was not recognized as a bench workflow; the "
                f"`Bench:` display-name convention or its parse changed")

    def test_pr_gate_workflows_are_not(self):
        paths = bsc.bench_workflow_paths()
        assert ".github/workflows/unit-tests.yml" not in paths
        assert ".github/workflows/static-checks.yml" not in paths

    def test_extended_path_constant_names_a_real_bench_workflow(self):
        """The routing below keys on this constant; a rename would unroute it."""
        assert bsc.EXTENDED_PATH in bsc.bench_workflow_paths()


class TestWorkflowStates:
    EXPECTED = {NIGHTLY, INTEGRATION, EXTENDED}

    def test_all_active_is_healthy(self):
        states = [wf(p) for p in self.EXPECTED]
        assert bsc.check_workflow_states(states, self.EXPECTED) == []

    def test_a_manually_disabled_workflow_is_a_problem(self):
        states = [wf(NIGHTLY), wf(INTEGRATION), wf(EXTENDED, "disabled_manually")]
        problems = bsc.check_workflow_states(states, self.EXPECTED)
        assert [p for p, _ in problems] == [EXTENDED]
        assert "disabled_manually" in problems[0][1]

    def test_an_inactivity_disabled_workflow_is_a_problem(self):
        states = [wf(NIGHTLY, "disabled_inactivity"), wf(INTEGRATION), wf(EXTENDED)]
        assert [p for p, _ in bsc.check_workflow_states(states, self.EXPECTED)] == [NIGHTLY]

    def test_a_workflow_github_does_not_know_is_a_problem(self):
        states = [wf(NIGHTLY), wf(EXTENDED)]
        problems = bsc.check_workflow_states(states, self.EXPECTED)
        assert [p for p, _ in problems] == [INTEGRATION]
        assert "not registered" in problems[0][1]

    def test_non_bench_workflows_are_ignored_whatever_their_state(self):
        states = [wf(p) for p in self.EXPECTED]
        states.append(wf(".github/workflows/unit-tests.yml", "disabled_manually"))
        assert bsc.check_workflow_states(states, self.EXPECTED) == []


class TestExtendedCadence:
    def test_last_saturday_is_healthy(self):
        assert bsc.check_extended_cadence([sched(3 * 24), sched(10 * 24)], now=NOW) == []

    def test_a_late_weekly_run_is_still_healthy(self):
        """Weekly plus the ~11h the scheduled queue has delayed a run."""
        assert bsc.check_extended_cadence([sched(7 * 24 + 11)], now=NOW) == []

    def test_a_missed_saturday_is_a_problem(self):
        problems = bsc.check_extended_cadence([sched(15 * 24)], now=NOW)
        assert len(problems) == 1 and "15.0 days old" in problems[0]

    def test_no_scheduled_run_at_all_is_a_problem(self):
        dispatch = dict(sched(24), event="workflow_dispatch")
        problems = bsc.check_extended_cadence([dispatch], now=NOW)
        assert len(problems) == 1 and "no scheduled" in problems[0]

    def test_a_dispatch_does_not_stand_in_for_the_schedule(self):
        """A hand run proves the workflow works, not that its cron fires."""
        dispatch = dict(sched(1), event="workflow_dispatch")
        assert bsc.check_extended_cadence([dispatch, sched(20 * 24)], now=NOW) != []


def _run_main(tmp_path, workflows, extended_runs):
    """Run the tool as the watchdog does: healthy nightly, given bench state."""
    now = datetime.now(timezone.utc)
    hour, minute = bsc.cron_hour_minute()
    newest = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    newest += timedelta(minutes=30)
    if newest > now:
        newest -= timedelta(days=1)
    nightly = [{"databaseId": i, "status": "completed", "event": "schedule",
                "createdAt": _iso(newest - timedelta(days=i))} for i in range(6)]
    files = {"runs.json": nightly, "scheduled.json": nightly,
             "workflows.json": workflows, "extended.json": extended_runs}
    for name, data in files.items():
        (tmp_path / name).write_text(json.dumps(data))
    return subprocess.run(
        [sys.executable, str(TOOL), "runs.json", "scheduled.json",
         "workflows.json", "extended.json"],
        cwd=tmp_path, capture_output=True, text=True)


def _all_bench_active():
    return [wf(p) for p in sorted(bsc.bench_workflow_paths())]


def _fresh_extended():
    return [sched(2 * 24, now=datetime.now(timezone.utc))]


class TestRouting:
    def test_healthy_bench_exits_0_and_writes_nothing(self, tmp_path):
        res = _run_main(tmp_path, _all_bench_active(), _fresh_extended())
        assert res.returncode == 0, res.stdout + res.stderr
        assert not (tmp_path / "problems.txt").exists()
        assert not (tmp_path / "problems-extended.txt").exists()

    def test_disabled_extended_goes_to_its_own_stream_only(self, tmp_path):
        states = [wf(p, "disabled_manually" if p == EXTENDED else "active")
                  for p in sorted(bsc.bench_workflow_paths())]
        res = _run_main(tmp_path, states, _fresh_extended())
        assert res.returncode == 1, res.stdout + res.stderr
        assert "disabled_manually" in (tmp_path / "problems-extended.txt").read_text()
        assert not (tmp_path / "problems.txt").exists(), (
            "an Extended problem on `bench-alert` would be closed by the next "
            "green nightly and refiled by the next watchdog run")

    def test_silent_extended_goes_to_its_own_stream_only(self, tmp_path):
        stale = [sched(20 * 24, now=datetime.now(timezone.utc))]
        res = _run_main(tmp_path, _all_bench_active(), stale)
        assert res.returncode == 1
        assert (tmp_path / "problems-extended.txt").exists()
        assert not (tmp_path / "problems.txt").exists()

    def test_other_disabled_bench_workflow_goes_to_the_nightly_stream(self, tmp_path):
        states = [wf(p, "disabled_manually" if p == INTEGRATION else "active")
                  for p in sorted(bsc.bench_workflow_paths())]
        res = _run_main(tmp_path, states, _fresh_extended())
        assert res.returncode == 1
        assert "integration-tests.yml" in (tmp_path / "problems.txt").read_text()
        assert not (tmp_path / "problems-extended.txt").exists()

    def test_the_two_argument_form_still_works(self, tmp_path):
        """Older callers pass only the nightly's two files."""
        _run_main(tmp_path, [], [])
        res = subprocess.run([sys.executable, str(TOOL), "runs.json", "scheduled.json"],
                             cwd=tmp_path, capture_output=True, text=True)
        assert res.returncode == 0, res.stdout + res.stderr
