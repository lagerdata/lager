# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The watchdog workflow must not carry its own copy of the tool's thresholds.

`bench-watchdog.yml` sets the environment for `tools/bench_schedule_check.py`,
and the two drifted apart the moment the tool was rewritten. #426 replaced an
interval-mean check with a lateness check, renaming two variables and changing
one default. The `env:` block was not rewritten with it, which left two defects
live on the first run:

  * `SCHEDULE_DRIFT_WARN_HOURS` was still set, still looked tunable, and was
    read by nothing. The check it used to configure no longer existed.
  * `SCHEDULE_GAP_ALERT_HOURS` was set to 26, overriding the tool's 36. The gap
    check exists to catch a night that certainly did not run, which is why its
    threshold sits well above the 24h nominal. At 26h it fires on a night that
    merely started late -- exactly the conflation the checks were split apart
    to avoid, and exactly what #430 reported: a 26.7h gap called a missed night.

Nothing could catch either one. The tool's own tests pass thresholds explicitly
and never read the workflow, so they cannot see a mismatch; `zizmor` and
`actionlint` check workflow syntax, not whether an `env:` name is one the
consuming script reads.

So the thresholds now live in exactly one file -- the tool, where each is
documented with its reasoning -- and this pins that. To retune a threshold,
change its default in `tools/bench_schedule_check.py`. Putting it back in the
workflow reintroduces a value declared in one file and consumed in another with
nothing asserting they agree, which is the defect class the watchdog itself
exists to catch.

Same enforcement style as `test_bench_power_on_blocks_match.py` and
`test_firewall_port_allowlist.py`: parse both sides, compare, and make an
exception a visible edit to this file.
"""

import ast
import pathlib

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bench-watchdog.yml"
TOOL = REPO_ROOT / "tools" / "bench_schedule_check.py"

#: Every threshold the tool reads is named in hours and ends this way. Matching
#: on the shape rather than on a hardcoded list is deliberate: a hardcoded list
#: would have to be kept in step with the tool, which is the failure being
#: prevented. `test_no_env_name_here_is_one_the_tool_reads` covers the case of a
#: future threshold that does not follow the convention.
THRESHOLD_SUFFIX = "_HOURS"

#: Non-threshold environment the step legitimately needs: `gh` credentials and
#: the run URL quoted into the alert body. These are consumed by the step's own
#: shell and by `tools/bench_alert.sh`, not by the schedule checker.
INFRASTRUCTURE_ENV = frozenset({"GH_TOKEN", "GH_REPO", "RUN_URL"})


def _workflow_env_names():
    """Every env name set anywhere in the workflow: top level, job, or step."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    names = set()
    names.update(doc.get("env") or {})
    for job in (doc.get("jobs") or {}).values():
        names.update(job.get("env") or {})
        for step in job.get("steps") or []:
            names.update(step.get("env") or {})
    return names


def _tool_env_reads():
    """{name: has_default} for every `os.environ.get("NAME", ...)` in the tool.

    Parsed rather than imported. The tool reads its environment into
    module-level constants at import time, so importing it here would capture
    this process's environment and tell us nothing about what it reads.
    """
    reads = {}
    for node in ast.walk(ast.parse(TOOL.read_text())):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ"
                and node.args
                and isinstance(node.args[0], ast.Constant)):
            reads[node.args[0].value] = len(node.args) > 1
    return reads


def test_the_parsers_found_something():
    """Guard the guard: either parser matching nothing would pass forever."""
    env_names = _workflow_env_names()
    assert INFRASTRUCTURE_ENV <= env_names, (
        f"the workflow env parse found {sorted(env_names)}, which is missing "
        f"some of {sorted(INFRASTRUCTURE_ENV)}. The step's env block moved or "
        f"was renamed, so every other test in this file is now vacuous."
    )
    reads = _tool_env_reads()
    thresholds = [n for n in reads if n.endswith(THRESHOLD_SUFFIX)]
    assert len(thresholds) >= 5, (
        f"the tool env parse found only {sorted(thresholds)}. "
        f"{TOOL.name} reads its thresholds some other way now, so the "
        f"comparison below is not checking anything."
    )


def test_the_workflow_sets_no_threshold():
    """A threshold set here is a second copy of a value the tool owns."""
    offenders = sorted(n for n in _workflow_env_names()
                       if n.endswith(THRESHOLD_SUFFIX))
    assert not offenders, (
        f"{WORKFLOW.name} sets {offenders}. Thresholds live in {TOOL.name}, "
        f"which documents each one's reasoning; setting a copy here is how "
        f"SCHEDULE_GAP_ALERT_HOURS came to override 36 with 26 and alarm on a "
        f"late night as though it were a missed one. Retune by changing the "
        f"default in {TOOL.name}."
    )


def test_no_env_name_here_is_one_the_tool_reads():
    """The same rule, not resting on the `_HOURS` naming convention."""
    read_here = sorted(_workflow_env_names() & set(_tool_env_reads()))
    assert not read_here, (
        f"{WORKFLOW.name} sets {read_here}, which {TOOL.name} reads. The tool "
        f"owns its own configuration; see {WORKFLOW.name}'s header comment."
    )


def test_every_threshold_the_tool_reads_has_a_default():
    """The mirror of the rule above, and what makes it safe.

    With nothing set in the workflow, a threshold read without a default would
    raise at import and take the watchdog down silently -- the check exits
    above 1, which the step reports as "could not run", not as a green bench.
    """
    undefaulted = sorted(name for name, has_default in _tool_env_reads().items()
                         if name.endswith(THRESHOLD_SUFFIX) and not has_default)
    assert not undefaulted, (
        f"{TOOL.name} reads {undefaulted} with no default. Nothing sets them: "
        f"{WORKFLOW.name} deliberately sets no thresholds, so every one the "
        f"tool reads must carry the value it should run at."
    )
