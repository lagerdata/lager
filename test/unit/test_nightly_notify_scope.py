# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Only a run of main may file or close the nightly `bench-alert` issue.

`nightly-bench.yml` can be dispatched on any branch, which is how a change is
validated on the bench before it merges. Its notify jobs had no ref or event
condition, so a branch run acted on the issue that describes main (#552):

  * a green branch dispatch closed the open alert on 2026-09-15, while
    scheduled nightlies had not run for five days;
  * a red branch dispatch filed an alert titled "Nightly bench is failing"
    about a failure that exists only on that branch.

Both jobs now require the scheduled run or a run of `refs/heads/main`. These
tests pin that, and pin the conditions each job already had, because the new
term is ANDed onto them and an edit that ORs it instead would re-open both
paths while still containing every expected substring.

`zizmor` and `actionlint` check that a condition is well formed, not what it
means, so nothing else catches a regression here short of a branch dispatch.
"""

import pathlib

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nightly-bench.yml"

#: The term both notify jobs must AND onto their existing condition.
MAIN_ONLY = "(github.event_name == 'schedule' || github.ref == 'refs/heads/main')"

NOTIFY_JOBS = ("notify-failure", "notify-recovery")


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _writes_issues(job):
    perms = job.get("permissions")
    return isinstance(perms, dict) and perms.get("issues") == "write"


def _condition(job):
    """The job's `if:`, without the `${{ }}` wrapper and with spaces collapsed."""
    text = " ".join(str(job.get("if", "")).split())
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text


def test_the_parser_found_the_notify_jobs():
    """Guard the guard: a renamed job would leave the checks below vacuous."""
    writers = sorted(name for name, job in _jobs().items() if _writes_issues(job))
    assert writers == sorted(NOTIFY_JOBS), (
        f"jobs with `issues: write` in {WORKFLOW.name}: {writers}. Every job "
        f"that can touch the bench-alert issue must be limited to main, so a "
        f"new one belongs in NOTIFY_JOBS and under the checks below."
    )


def test_every_job_that_writes_issues_is_limited_to_main():
    for name, job in _jobs().items():
        if not _writes_issues(job):
            continue
        condition = _condition(job)
        assert condition.endswith(" && " + MAIN_ONLY), (
            f"{name} runs on `if: {condition}`. It must end with "
            f"`&& {MAIN_ONLY}`, or a dispatch on a feature branch can file or "
            f"close the alert that describes main (#552)."
        )


def test_recovery_still_requires_both_children_to_succeed():
    job = _jobs()["notify-recovery"]
    assert _condition(job).startswith("success() && "), (
        "notify-recovery must close the alert only on a fully green night"
    )
    assert sorted(job["needs"]) == ["integration", "lifecycle"]


def test_failure_still_fires_on_a_skipped_child_and_ignores_cancellation():
    job = _jobs()["notify-failure"]
    condition = _condition(job)
    assert condition.startswith("!cancelled() && "), condition
    assert (
        "(needs.lifecycle.result != 'success' || "
        "needs.integration.result != 'success')"
    ) in condition, condition
    assert sorted(job["needs"]) == ["integration", "lifecycle"]
