# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Every bench job releases a lock left by a dead run of this repo -- and only that.

A re-run of a failed chain found the failed attempt's CI lock still held 40
minutes later; its first `lager nets state` was refused and the night was lost.
Only Bench: Extended released a stale lock at job start, and it forced EVERY
lock, so a person holding the bench to work on it would have lost it too.

The step now lives, byte-identical, in every bench workflow before its
connectivity check, and releases a lock only when the holder is a run of this
repository. These tests pin the placement and run the step's own shell against
a stand-in `lager` to pin the decision.
"""

import os
import pathlib
import shutil
import stat
import subprocess

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
BENCH = ("update-regression.yml", "integration-tests.yml", "bench-extended.yml")
STEP = "Release a box lock left by a dead CI run"
CHECK = "Verify box connectivity"
REPO = "lagerdata/lager"


def _steps(name):
    doc = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    (job,) = [j for j in doc["jobs"].values() if "self-hosted" in str(j.get("runs-on"))]
    return job["steps"]


def _release_run(name):
    return next(s for s in _steps(name) if s.get("name") == STEP)["run"]


class TestPlacement:
    @pytest.mark.parametrize("name", BENCH)
    def test_the_step_runs_before_the_connectivity_check(self, name):
        names = [s.get("name") for s in _steps(name)]
        assert STEP in names, f"{name} has no '{STEP}' step"
        assert names.index(STEP) < names.index(CHECK), (
            f"{name}: the release must run before '{CHECK}', or a stale lock "
            f"can fail the job before it is released")

    def test_the_step_is_byte_identical_everywhere(self):
        bodies = {name: _release_run(name) for name in BENCH}
        assert len(set(bodies.values())) == 1, (
            "the stale-lock release differs between bench workflows; keep one "
            "copy of the rule, edited in all three")

    @pytest.mark.parametrize("name", BENCH)
    def test_no_bench_job_forces_an_unlock_unconditionally_at_start(self, name):
        """The old Extended step: `unlock --force` before anything else."""
        for step in _steps(name):
            if step.get("name") == CHECK:
                break
            run = step.get("run") or ""
            if "unlock" in run and "--force" in run:
                assert step.get("name") == STEP, (
                    f"{name}: '{step.get('name')}' forces an unlock before "
                    f"the connectivity check without checking the holder")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
class TestDecision:
    """The step's own shell, against a fake `lager` that records its calls."""

    def _run(self, tmp_path, hello_out, hello_rc=0):
        log = tmp_path / "calls"
        fake = tmp_path / "lager"
        fake.write_text(
            "#!/bin/bash\n"
            f'echo "$*" >> "{log}"\n'
            'if [ "$1" = hello ]; then\n'
            f"  cat <<'EOF'\n{hello_out}\nEOF\n"
            f"  exit {hello_rc}\n"
            "fi\n"
            "exit 0\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}",
                   LAGER_BOX="MASTER", GITHUB_REPOSITORY=REPO)
        res = subprocess.run(["bash", "-e", "-o", "pipefail", "-c",
                              _release_run(BENCH[0])],
                             env=env, capture_output=True, text=True)
        calls = log.read_text().splitlines() if log.exists() else []
        return res, calls

    def test_a_dead_run_of_this_repo_is_released(self, tmp_path):
        res, calls = self._run(tmp_path, (
            f"Note: Box 'MASTER' is locked by github {REPO} run 37057279741 "
            "job box-lifecycle on MASTER; running read-only."))
        assert res.returncode == 0, res.stdout + res.stderr
        assert "boxes unlock --box MASTER --force" in calls
        assert "Stale bench lock" in res.stdout

    def test_a_persons_lock_is_left_alone(self, tmp_path):
        res, calls = self._run(tmp_path, (
            "Note: Box 'MASTER' is locked by someone; running read-only."))
        assert res.returncode == 0
        assert not any("unlock" in c for c in calls)

    def test_another_repos_ci_lock_is_left_alone(self, tmp_path):
        res, calls = self._run(tmp_path, (
            "Note: Box 'MASTER' is locked by github other/repo run 1 job x on "
            "MASTER; running read-only."))
        assert res.returncode == 0
        assert not any("unlock" in c for c in calls)

    def test_an_unlocked_box_is_left_alone(self, tmp_path):
        res, calls = self._run(tmp_path, "Lager box MASTER is online")
        assert res.returncode == 0
        assert not any("unlock" in c for c in calls)

    def test_an_unreachable_box_does_not_fail_this_step(self, tmp_path):
        """The connectivity check after it is the step that fails loudly."""
        res, calls = self._run(tmp_path, "Error: cannot reach box", hello_rc=1)
        assert res.returncode == 0, res.stdout + res.stderr
        assert not any("unlock" in c for c in calls)
