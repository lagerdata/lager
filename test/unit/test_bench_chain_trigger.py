# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The bench chain runs one chain at a time, and merges reach the bench only
through it.

Two rules, each learned from a run that produced no hardware result:

1. **A chain holds its own concurrency group for its whole run.** The children
   serialize on `hardware-ci-<box>`, which is released between lifecycle and
   integration. On 2026-09-28 a second chain took the slot there: its lifecycle
   deployed a branch, and the first chain's integration then failed its
   ref-guard against a box that had been moved off the commit it was testing.
   The chain group must also DIFFER from the children's, or the caller waits on
   a child that wants the slot it holds -- a deadlock.

2. **Only the chain triggers on push, and only for a release tag.** A
   push-triggered leaf tests whatever the last run left on the box; that is why
   integration-tests.yml lost its push trigger. The chain deploys the pushed
   commit before testing it, so the push trigger belongs there and nowhere
   else. It fires on `v*` tags, not on merges to main: a chain on every merge
   kept the one bench busy for most of the working day. A tag run must wait
   for its own box image, which lifecycle's release-tag install needs.

`actionlint` and `zizmor` check that these keys are well formed, not what they
mean, so nothing else catches a regression here short of a lost night.
"""

import pathlib

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CHAIN = "nightly-bench.yml"
CHILDREN = ("update-regression.yml", "integration-tests.yml")
LEAVES = CHILDREN + ("bench-extended.yml",)


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(doc):
    # PyYAML reads the bare key `on` as boolean True.
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {k: None for k in on}
    return on or {}


def _group(doc):
    concurrency = doc.get("concurrency")
    assert isinstance(concurrency, dict), "no workflow-level concurrency block"
    return concurrency


class TestOneChainAtATime:
    def test_the_chain_holds_a_group_for_its_whole_run(self):
        group = _group(_load(CHAIN))
        assert "bench-chain-" in str(group.get("group")), group
        assert group.get("cancel-in-progress") is False, (
            "a newer chain must never kill one mid-measurement")

    def test_the_chain_group_differs_from_every_child_group(self):
        chain = str(_group(_load(CHAIN))["group"])
        for child in CHILDREN:
            child_group = str(_group(_load(child))["group"])
            assert "hardware-ci-" in child_group, (child, child_group)
            assert chain != child_group, (
                f"{CHAIN} and {child} share `{chain}`: the caller would hold the "
                f"slot while waiting on a child that wants it -- a deadlock")

    def test_the_chain_group_names_the_same_box_as_the_children(self):
        """Two boxes must not share a chain slot, and one box must not get two."""
        box_expr = "${{ vars.LAGER_BOX || 'MASTER' }}"
        assert box_expr in str(_group(_load(CHAIN))["group"])
        for child in CHILDREN:
            assert box_expr in str(_group(_load(child))["group"]), child


class TestOnlyTheChainRunsOnPush:
    def test_the_chain_runs_on_push_of_release_tags_only(self):
        push = _triggers(_load(CHAIN)).get("push")
        assert push == {"tags": ["v*"]}, (
            f"the chain's push trigger is {push!r}. It must be release tags only: "
            f"a branch filter brings back a bench run per merge, and a path "
            f"filter is not evaluated for a tag push, so it only misleads")

    def test_the_chain_still_runs_nightly_and_on_dispatch(self):
        triggers = _triggers(_load(CHAIN))
        assert triggers.get("schedule"), "the chain lost its nightly cron"
        assert "workflow_dispatch" in triggers, "a branch can no longer be bench-tested"

    def test_no_leaf_runs_on_push_or_pull_request(self):
        for leaf in LEAVES:
            triggers = _triggers(_load(leaf))
            assert "push" not in triggers, (
                f"{leaf} triggers on push, but nothing in it deploys first; a "
                f"push run tests whatever the last run left on the box. Put "
                f"push triggers on {CHAIN}.")
            assert "pull_request" not in triggers and "pull_request_target" not in triggers, (
                f"{leaf}: a fork PR must never execute code on the bench")

    def test_the_chain_is_not_triggered_by_pull_requests(self):
        triggers = _triggers(_load(CHAIN))
        assert "pull_request" not in triggers and "pull_request_target" not in triggers


class TestATagRunWaitsForItsBoxImage:
    """Lifecycle installs the newest tag from its GHCR image. On a tag run that
    is the tag just pushed, and box-image-publish.yml is still building it."""

    def _job(self):
        return _load(CHAIN)["jobs"]["box-image"]

    def test_lifecycle_waits_for_the_box_image_job(self):
        assert _load(CHAIN)["jobs"]["lifecycle"].get("needs") == "box-image"

    def test_the_wait_runs_hosted_and_holds_no_bench(self):
        job = self._job()
        assert job["runs-on"] == "ubuntu-latest", job["runs-on"]
        assert job.get("permissions") == {}, job.get("permissions")
        assert job.get("timeout-minutes"), "an unbounded wait would hold the chain group"

    def test_the_wait_acts_on_tag_runs_only(self):
        """A nightly or a dispatch must pass straight through: its newest tag
        was published long ago, and a missing image there is lifecycle's to
        report."""
        steps = self._job()["steps"]
        assert steps and all(
            " ".join(str(step.get("if", "")).split()) == "${{ github.ref_type == 'tag' }}"
            for step in steps), steps

    def test_the_wait_probes_the_tag_under_test(self):
        step = self._job()["steps"][0]
        assert step["env"]["TAG"] == "${{ github.ref_name }}"
        assert "${{" not in step["run"], "pass expressions through env, not into the script"
        assert "manifests/${TAG}" in step["run"]


class TestFailureAlertNamesTheSuspects:
    def test_notify_failure_can_read_run_history(self):
        job = _load(CHAIN)["jobs"]["notify-failure"]
        assert job["permissions"].get("actions") == "read", (
            "the alert looks up the last green run's commit; without "
            "actions: read that lookup fails and the range is silently dropped")

    def test_notify_failure_links_the_commit_range(self):
        body = "\n".join(s.get("run", "") for s in
                         _load(CHAIN)["jobs"]["notify-failure"]["steps"])
        assert "status=success" in body and "branch=main" in body
        assert "/compare/" in body
