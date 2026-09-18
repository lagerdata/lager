# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Only a run of main may file or close a bench alert issue.

`nightly-bench.yml` and `bench-extended.yml` can each be dispatched on any
branch, which is how a change is validated on the bench before it merges. A
notify job with no ref or event condition lets that branch run act on an issue
that describes main.

The nightly did exactly that (#552):

  * a green branch dispatch closed the open alert on 2026-09-15, while
    scheduled nightlies had not run for five days;
  * a red branch dispatch filed an alert titled "Nightly bench is failing"
    about a failure that exists only on that branch.

Its two notify jobs were limited to the scheduled run or a run of
`refs/heads/main`. `bench-extended.yml` has a notify job of its own, writing to
`bench-alert-extended`, and it was left out: a red dispatch of the extended
bench on a feature branch still filed against main. It carries the same clause
now, and this file covers every workflow job that can write an issue, so the
next one cannot be left out the same way.

These tests pin the clause, and pin the conditions each job already had,
because the new term is ANDed onto them and an edit that ORs it instead would
re-open both paths while still containing every expected substring.

`zizmor` and `actionlint` check that a condition is well formed, not what it
means, so nothing else catches a regression here short of a branch dispatch.
"""

import pathlib

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The term every such job must AND onto its existing condition.
MAIN_ONLY = "(github.event_name == 'schedule' || github.ref == 'refs/heads/main')"

#: Every job that can write an issue and must be limited to main.
MAIN_ONLY_JOBS = {
    "nightly-bench.yml": ("notify-failure", "notify-recovery"),
    "bench-extended.yml": ("notify-failure",),
}

#: Jobs that can write an issue and are NOT limited to main, each with its reason.
#: An entry here is a decision somebody made; an issue-writing job that is in
#: neither table fails test_every_issue_writer_is_accounted_for.
NOT_LIMITED_ON_PURPOSE = {
    "bench-watchdog.yml": {
        "watchdog": (
            "It reports on the nightly's cron, which belongs to main whatever ref "
            "the watchdog itself runs from, and no change is validated by "
            "dispatching it on a branch."
        ),
    },
}


def _jobs(workflow):
    return yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"]


def _writes_issues(job):
    perms = job.get("permissions")
    return isinstance(perms, dict) and perms.get("issues") == "write"


def _condition(job):
    """The job's `if:`, without the `${{ }}` wrapper and with spaces collapsed."""
    text = " ".join(str(job.get("if", "")).split())
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text


def test_every_issue_writer_is_accounted_for():
    """Guard the guard: scan EVERY workflow, not the ones someone remembered.

    The extended bench's notify job was missed because the first version of
    this file read nightly-bench.yml and nothing else.
    """
    found = {}
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        writers = sorted(name for name, job in _jobs(path.name).items() if _writes_issues(job))
        if writers:
            found[path.name] = writers
    expected = {name: sorted(jobs) for name, jobs in MAIN_ONLY_JOBS.items()}
    expected.update({name: sorted(jobs) for name, jobs in NOT_LIMITED_ON_PURPOSE.items()})
    assert found == expected, (
        f"jobs with `issues: write`: {found}. Every one must be limited to main "
        f"(MAIN_ONLY_JOBS) or be listed in NOT_LIMITED_ON_PURPOSE with its reason."
    )


def test_an_exemption_always_says_why():
    for workflow, jobs in NOT_LIMITED_ON_PURPOSE.items():
        for name, reason in jobs.items():
            assert len(reason.split()) >= 8, f"{workflow}:{name} needs a real reason"


@pytest.mark.parametrize("workflow", sorted(MAIN_ONLY_JOBS))
def test_the_parser_found_the_notify_jobs(workflow):
    """A renamed job would leave the checks below vacuous for that workflow."""
    writers = sorted(name for name, job in _jobs(workflow).items() if _writes_issues(job))
    assert writers == sorted(MAIN_ONLY_JOBS[workflow]), (
        f"jobs with `issues: write` in {workflow}: {writers}. Every job that can "
        f"touch a bench alert issue must be limited to main, so a new one belongs "
        f"in MAIN_ONLY_JOBS and under the checks below."
    )


@pytest.mark.parametrize("workflow, name", [
    (workflow, name) for workflow, jobs in sorted(MAIN_ONLY_JOBS.items()) for name in jobs
])
def test_every_job_that_writes_issues_is_limited_to_main(workflow, name):
    condition = _condition(_jobs(workflow)[name])
    assert condition.endswith(" && " + MAIN_ONLY), (
        f"{workflow}: {name} runs on `if: {condition}`. It must end with "
        f"`&& {MAIN_ONLY}`, or a dispatch on a feature branch can file or "
        f"close the alert that describes main (#552)."
    )


def test_recovery_still_requires_both_children_to_succeed():
    job = _jobs("nightly-bench.yml")["notify-recovery"]
    assert _condition(job).startswith("success() && "), (
        "notify-recovery must close the alert only on a fully green night"
    )
    assert sorted(job["needs"]) == ["integration", "lifecycle"]


def test_failure_still_fires_on_a_skipped_child_and_ignores_cancellation():
    job = _jobs("nightly-bench.yml")["notify-failure"]
    condition = _condition(job)
    assert condition.startswith("!cancelled() && "), condition
    assert (
        "(needs.lifecycle.result != 'success' || "
        "needs.integration.result != 'success')"
    ) in condition, condition
    assert sorted(job["needs"]) == ["integration", "lifecycle"]


def test_the_extended_bench_still_fires_on_any_red_run_and_ignores_cancellation():
    job = _jobs("bench-extended.yml")["notify-failure"]
    # The whole condition, so that the new term can only have been ANDed on.
    assert _condition(job) == (
        "!cancelled() && needs.extended.result != 'success' && " + MAIN_ONLY
    )
    assert job["needs"] == ["extended"]
