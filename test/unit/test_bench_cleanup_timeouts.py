# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Every `always()` step on the self-hosted bench must be bounded.

The bench workflows all serialize on one concurrency group --
`hardware-ci-${{ vars.LAGER_BOX || 'MASTER' }}` with `cancel-in-progress: false`
-- because there is one physical bench and a run must never be killed
mid-measurement. That is the right policy, and it is what makes an unbounded
cleanup step expensive: while it hangs, no other bench workflow can start.

A cleanup step guarded by `if: always()` is the step that runs *after* something
has already gone wrong, so it runs precisely when the box is in the state most
likely to hang it. `|| true` and `continue-on-error` guard the exit code; they
do not guard the wall clock. Without `timeout-minutes` the only bound left is
the job budget -- 45, 75 and 90 minutes across the three bench workflows -- and
the group is held for every minute of it.

That is not hypothetical: in `Bench: Nightly` run 32408637976, `Best-effort
J-Link cleanup` ran from 20:07:29 to 20:27:09, twenty minutes after the suite it
was cleaning up after had already timed out (#322).

`timeout-minutes` alone would trade a hang for a red run, since a step killed by
its timeout fails the job. So the rule is both halves: a bench `always()` step
must carry `timeout-minutes` AND be best-effort under it, which is what
`continue-on-error: true` means. A step that is worth running unconditionally
but not worth failing the job over is exactly the shape being described.

This is the same enforcement style as `test_no_global_os_path_patches.py` and
`test_sudoers_contract.py`: scan the tree, allow nothing, and make an exception
a visible edit to this file.
"""

import pathlib
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The runner label that means "the one physical bench". Only jobs that land
#: here hold the serialized hardware group, so only they are in scope --
#: `static-checks` and `rust-checks` use `always()` to report every check in one
#: pass on a GitHub-hosted runner, where a hang costs a hosted minute and
#: nothing else's turn.
BENCH_LABEL = "self-hosted"

#: Steps exempt from the rule, as `(workflow file, job id, step name)`.
#: Empty on purpose: adding an entry should be an argument in a diff, not a
#: silent omission. If a bench cleanup step genuinely cannot be bounded, say
#: why here.
EXEMPT = frozenset()


def _runs_on_bench(job):
    runs_on = job.get("runs-on")
    if isinstance(runs_on, str):
        return runs_on == BENCH_LABEL
    if isinstance(runs_on, (list, tuple)):
        return BENCH_LABEL in runs_on
    if isinstance(runs_on, dict):  # `runs-on: {group:, labels:}`
        labels = runs_on.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        return BENCH_LABEL in labels
    return False


def _bench_always_steps():
    """Yield ``(path, job_id, step)`` for every ``always()`` step on the bench."""
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        for job_id, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict) or not _runs_on_bench(job):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                if "always()" in str(step.get("if", "") or ""):
                    yield path, job_id, step


def _name(step):
    return step.get("name") or step.get("uses") or (step.get("run", "") or "")[:60]


class TestBenchCleanupStepsAreBounded(unittest.TestCase):
    def test_the_scan_finds_something(self):
        """Guard the guard: a parser that silently matches nothing passes."""
        found = list(_bench_always_steps())
        self.assertGreater(
            len(found), 0,
            "no `always()` steps found on any self-hosted bench job — the "
            "workflows moved or the parse broke, and this file is now testing "
            "nothing",
        )

    def test_every_bench_always_step_has_a_timeout(self):
        offenders = [
            f"{path.name} :: {job_id} :: {_name(step)}"
            for path, job_id, step in _bench_always_steps()
            if (path.name, job_id, _name(step)) not in EXEMPT
            and "timeout-minutes" not in step
        ]
        self.assertEqual(
            offenders, [],
            "these bench steps run on `always()` with no `timeout-minutes`, so "
            "a hang in one holds the serialized hardware-ci group for the whole "
            "job budget:\n  " + "\n  ".join(offenders),
        )

    def test_every_bench_always_step_is_best_effort(self):
        """A bounded step still fails the job unless it says it may fail."""
        offenders = [
            f"{path.name} :: {job_id} :: {_name(step)}"
            for path, job_id, step in _bench_always_steps()
            if (path.name, job_id, _name(step)) not in EXEMPT
            and step.get("continue-on-error") is not True
        ]
        self.assertEqual(
            offenders, [],
            "these bench steps run on `always()` without "
            "`continue-on-error: true`, so the timeout that bounds them would "
            "turn an otherwise-green run red:\n  " + "\n  ".join(offenders),
        )

    def test_the_bench_group_is_still_serialized(self):
        """The premise. If this ever stops holding, the rule needs rewriting."""
        checked = 0
        for path in sorted(WORKFLOWS.glob("*.y*ml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                continue
            for job_id, job in (doc.get("jobs") or {}).items():
                if not isinstance(job, dict) or not _runs_on_bench(job):
                    continue
                concurrency = job.get("concurrency") or doc.get("concurrency")
                self.assertIsInstance(
                    concurrency, dict,
                    f"{path.name}::{job_id} runs on the bench with no "
                    f"concurrency group — two runs could take it at once",
                )
                self.assertIs(
                    concurrency.get("cancel-in-progress"), False,
                    f"{path.name}::{job_id} may be cancelled mid-run",
                )
                checked += 1
        self.assertGreater(checked, 0, "no bench jobs found to check")


if __name__ == "__main__":
    unittest.main()
