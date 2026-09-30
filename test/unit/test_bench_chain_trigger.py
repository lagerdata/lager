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

2. **Only the chain triggers on push.** A push-triggered leaf tests whatever the
   last run left on the box; that is why integration-tests.yml lost its push
   trigger. The chain deploys the pushed commit before testing it, so the push
   trigger belongs there and nowhere else. Its docs-only filter must never
   swallow a change the bench can observe.

`actionlint` and `zizmor` check that these keys are well formed, not what they
mean, so nothing else catches a regression here short of a lost night.
"""

import pathlib
import re

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


def _glob_to_regex(pattern):
    """GitHub path-filter glob -> regex: `**` crosses `/`, `*` does not."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def _ignored(path, patterns):
    return any(_glob_to_regex(p).match(path) for p in patterns)


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
    def test_the_chain_runs_on_push_to_main_only(self):
        push = _triggers(_load(CHAIN)).get("push")
        assert isinstance(push, dict), "the chain lost its push trigger"
        assert push.get("branches") == ["main"], push

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


class TestDocsFilter:
    def _patterns(self):
        push = _triggers(_load(CHAIN))["push"]
        assert isinstance(push, dict), "the chain lost its push trigger"
        assert "paths" not in push, "an allow-list would silently skip new code paths"
        return push.get("paths-ignore") or []

    def test_changes_the_bench_can_observe_always_run(self):
        patterns = self._patterns()
        for path in ("cli/commands/development/python.py",
                     "box/lager/python/service.py",
                     "box/start_box.sh",
                     "box.Dockerfile",
                     ".github/workflows/nightly-bench.yml",
                     ".github/workflows/integration-tests.yml",
                     "tools/bench_suite_gate.sh",
                     "test/api/power/test_supply_Rigol_DP821.py",
                     "test/integration/communication/uart.sh",
                     "setup.py"):
            assert not _ignored(path, patterns), (
                f"a push touching only {path} would skip the bench")

    def test_docs_only_changes_are_skipped(self):
        """Guard the guard: a filter that matches nothing is not a filter."""
        patterns = self._patterns()
        for path in ("docs/source/reference/cli/locking.mdx",
                     "CHANGELOG.md",
                     "test/COVERAGE.md",
                     ".github/workflows/README.md"):
            assert _ignored(path, patterns), f"{path} should not cost a bench run"


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
