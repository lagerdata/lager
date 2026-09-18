# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The three bench workflows power the instruments on through ONE composite
action, and that action is the only place the procedure lives.

`integration-tests.yml`, `update-regression.yml` and `bench-extended.yml` each
carried the same ninety-line power-on script. The copies drifted: a fix that
added the relay-write retry and put the Keithley 2281S behind KEITHLEY_PRESENT
landed in one of them, the nightly ran another first, and the run dispatched
against the very commit that added the gate failed waiting for an instrument
that was off the bench. `test_bench_power_on_blocks_match.py` then compared the
three copies byte for byte.

With one copy there is nothing to compare, and that test is gone (#432). What
can still go wrong is different, and is what this file pins:

  * a caller grows a script of its own again, beside or instead of the action;
  * a caller stops passing an input, or passes one the others do not;
  * the per-caller keys move -- bench-extended.yml needs `id`, `if` and
    `continue-on-error` on the step, and the other two must have none;
  * the Keithley gate, which is what the outage was about, leaves the action;
  * an input gets expanded inside `run:`, where a value becomes shell.

A workflow only proves itself by running, and these run on the serialized
bench. Reading the YAML is what a unit test can do, so it reads all of it.
"""

import pathlib
import re

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "bench-power-on"
ACTION = ACTION_DIR / "action.yml"

USES = "./.github/actions/bench-power-on"
STEP_NAME = "Power on bench instruments (AC relays)"
CALLERS = ("integration-tests.yml", "update-regression.yml", "bench-extended.yml")

#: What differs per caller stays on the caller's step. bench-extended.yml runs
#: on when power-on fails and reads `steps.power-on.outcome` afterwards; the
#: other two set none of these ON PURPOSE, so that a bench that cannot be
#: powered fails once, here, and not as seven suites failing one by one.
PER_CALLER_KEYS = {
    "integration-tests.yml": {},
    "update-regression.yml": {},
    "bench-extended.yml": {
        "id": "power-on",
        "if": "steps.relay-nets.outcome == 'success'",
        "continue-on-error": True,
    },
}

EXPECTED_WITH = {
    "box": "${{ env.LAGER_BOX }}",
    "rigol-power-net": "${{ vars.RIGOL_POWER_NET || 'RIGOL_POWER' }}",
    "keithley-power-net": "${{ vars.KEITHLEY_POWER_NET || 'KEITHLEY_POWER' }}",
    "probe-supply-net": "${{ vars.RIGOL_CH1_NET || 'supply2' }}",
    "keithley-present": "${{ vars.KEITHLEY_PRESENT }}",
}


def _workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _action():
    return yaml.safe_load(ACTION.read_text())


def _power_on(name):
    """``(job, steps, index)`` of the one power-on step in a caller."""
    hits = []
    for job in _workflow(name)["jobs"].values():
        steps = job.get("steps") or []
        hits += [(job, steps, i) for i, step in enumerate(steps) if step.get("name") == STEP_NAME]
    assert len(hits) == 1, f"{name}: {len(hits)} steps named {STEP_NAME!r}"
    return hits[0]


def _action_step():
    steps = _action()["runs"]["steps"]
    assert len(steps) == 1, "the action is one step; a second one needs its own tests"
    return steps[0]


@pytest.mark.parametrize("caller", CALLERS)
class TestEachCaller:
    def test_it_calls_the_action_and_has_no_script_of_its_own(self, caller):
        _job, steps, i = _power_on(caller)
        assert steps[i].get("uses") == USES
        for key in ("run", "shell", "env"):
            assert key not in steps[i], f"{caller}: the step still has its own `{key}:`"

    def test_it_passes_every_input_from_the_same_expressions(self, caller):
        _job, steps, i = _power_on(caller)
        assert steps[i].get("with") == EXPECTED_WITH

    def test_it_sets_exactly_the_keys_that_are_its_own(self, caller):
        _job, steps, i = _power_on(caller)
        own = {k: v for k, v in steps[i].items() if k not in ("name", "uses", "with")}
        assert own == PER_CALLER_KEYS[caller]

    def test_the_box_it_passes_is_set_on_the_job(self, caller):
        # `box: ${{ env.LAGER_BOX }}` reads the JOB's env. A caller that moved
        # LAGER_BOX onto a step would pass the action an empty string.
        job, _steps, _i = _power_on(caller)
        assert "LAGER_BOX" in (job.get("env") or {}), caller

    def test_the_repository_is_checked_out_first(self, caller):
        # A local action is read from the workspace, so it does not exist
        # until the checkout has run.
        _job, steps, i = _power_on(caller)
        earlier = [str(step.get("uses", "")) for step in steps[:i]]
        assert any(u.startswith("actions/checkout@") for u in earlier), caller

    def test_lager_is_installed_first(self, caller):
        _job, steps, i = _power_on(caller)
        earlier = " ".join(str(step.get("run", "")) for step in steps[:i])
        assert re.search(r"pip install\b[^\n]*(-e|lager)", earlier), caller


class TestTheAction:
    def test_it_is_a_composite_action_that_declares_every_input_a_caller_passes(self):
        action = _action()
        assert action["runs"]["using"] == "composite"
        assert sorted(action["inputs"]) == sorted(EXPECTED_WITH)
        required = {name for name, spec in action["inputs"].items() if spec.get("required")}
        assert required == set(EXPECTED_WITH) - {"keithley-present"}
        # Unset, the variable reaches the action as an empty string, which
        # must mean "not on the bench".
        assert action["inputs"]["keithley-present"].get("default") == ""

    def test_every_input_reaches_the_script_through_env_and_none_is_expanded_in_run(self):
        step = _action_step()
        assert step["shell"] == "bash"
        assert step["env"] == {
            "LAGER_BOX": "${{ inputs.box }}",
            "RIGOL_POWER_NET": "${{ inputs.rigol-power-net }}",
            "KEITHLEY_POWER_NET": "${{ inputs.keithley-power-net }}",
            "PROBE_SUPPLY_NET": "${{ inputs.probe-supply-net }}",
            "KEITHLEY_PRESENT": "${{ inputs.keithley-present }}",
        }
        # An expression inside `run:` is pasted into the script as text, so a
        # value could become shell. Through `env:` it can only be a value.
        assert "${{" not in step["run"]

    def test_the_script_reads_nothing_the_action_does_not_give_it(self):
        step = _action_step()
        used = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]+)", step["run"]))
        assert used == set(step["env"]), used ^ set(step["env"])

    def test_the_keithley_is_gated_not_hardcoded(self):
        # The outage: one copy waited 180 s for an instrument that was off the
        # bench. The base list names the two always-present instruments, and
        # the Keithley joins it only behind the gate.
        run = _action_step()["run"]
        assert "expected=(Rigol_DP821 Rigol_MSO5204)" in run
        assert 'if [ "${KEITHLEY_PRESENT:-}" = "true" ]; then' in run
        assert "expected+=(Keithley_2281S)" in run
        assert run.index('"${KEITHLEY_PRESENT:-}"') < run.index("expected+=(Keithley_2281S)")

    def test_the_three_phases_are_all_there(self):
        run = _action_step()["run"]
        drive = run.index('lager gpo "$net" high --box "$LAGER_BOX"')
        enumerate_ = run.index('lager instruments --box "$LAGER_BOX"')
        ready = run.index('lager supply "$PROBE_SUPPLY_NET" state --box "$LAGER_BOX"')
        assert drive < enumerate_ < ready
        assert len(run.splitlines()) > 50

    def test_the_file_carries_the_license_header_every_yaml_here_does(self):
        lines = ACTION.read_text().splitlines()
        assert lines[0].startswith("# Copyright ")
        assert lines[1] == "# SPDX-License-Identifier: Apache-2.0"

    def test_the_action_uses_no_other_action(self):
        # Dependabot's github-actions updates do not scan this directory, so a
        # pinned `uses:` in here would never be bumped.
        assert all("uses" not in step for step in _action()["runs"]["steps"])


class TestThereIsOneCopy:
    def test_no_workflow_drives_the_relays_high_itself(self):
        offenders = [
            path.name for path in sorted(WORKFLOWS.glob("*.y*ml"))
            if 'lager gpo "$net" high' in path.read_text()
        ]
        assert offenders == [], f"a power-on script is back in: {offenders}"

    def test_the_scan_above_would_see_a_copy(self):
        assert 'lager gpo "$net" high' in ACTION.read_text()

    def test_the_old_comparison_test_is_gone(self):
        # It compared three copies. Left behind, it would assert something
        # that can no longer drift -- or fail on a block that is not there.
        old = REPO_ROOT / "test" / "unit" / "box" / "test_bench_power_on_blocks_match.py"
        assert not old.exists()

    def test_nothing_else_is_in_the_action_directory(self):
        assert sorted(p.name for p in ACTION_DIR.iterdir()) == ["action.yml"]


class TestTheLintReachesIt:
    def test_zizmor_is_given_the_actions_directory_as_well_as_the_workflows(self):
        # actionlint reads workflows only, so zizmor reads an action only
        # if its directory is on the command line.
        static = _workflow("static-checks.yml")
        runs = [str(step.get("run", "")) for job in static["jobs"].values()
                for step in job.get("steps") or []]
        zizmor = [" ".join(run.split()) for run in runs if re.search(r"^\s*zizmor ", run, re.M)]
        assert len(zizmor) == 1, zizmor
        assert ".github/workflows/" in zizmor[0]
        assert ".github/actions/" in zizmor[0]

    def test_the_actions_scripts_are_shellchecked(self):
        # Nothing else lints a composite action's `run:`: the ShellCheck step
        # reads *.sh files, and actionlint runs with its shellcheck off.
        static = _workflow("static-checks.yml")
        steps = [step for job in static["jobs"].values() for step in job.get("steps") or []]
        [step] = [s for s in steps if "composite actions" in str(s.get("name", ""))]
        run = step["run"]
        assert ".github/actions" in run
        assert 'step.get("shell") != "bash"' in run
        assert re.search(r"^\s*shellcheck -S warning ", run, re.M)
        assert step.get("if") == "always()"

    def test_every_action_step_is_a_bash_script(self):
        # The lint above refuses any other shell, so pin it here too, where a
        # failure names the action rather than a CI log line.
        for step in yaml.safe_load(ACTION.read_text())["runs"]["steps"]:
            if "run" in step:
                assert step.get("shell") == "bash", step.get("name")
