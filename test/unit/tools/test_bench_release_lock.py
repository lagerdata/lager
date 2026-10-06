# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`tools/bench_release_lock.sh` must report a lock it could not release.

The bench workflows released their box lock with
`lager boxes unlock --force 2>/dev/null || true`. When the box's lager service
was down the unlock failed without a word, the lock came back with the
service, and the run's own re-run met it at its first box command (#639).

These tests drive the real script against a fake `lager` that plays back a
script of box states, one per call:

    hello:<state>   `lager hello` answers: unlocked, ours, theirs, or down
    unlock:<ok|fail>
"""

import pathlib
import re
import shutil
import subprocess
import textwrap

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
HELPER = REPO_ROOT / "tools" / "bench_release_lock.sh"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

REPO = "lagerdata/lager"
OURS = f"github {REPO} run 123 job box-lifecycle on MASTER"
THEIRS = "alice"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def fake_lager(tmp_path):
    """A `lager` stand-in. Each call pops its answer from a script file and
    records what it was asked."""

    def make(*answers):
        script = tmp_path / "answers"
        script.write_text("\n".join(answers) + "\n")
        calls = tmp_path / "calls"
        calls.write_text("")
        fake = tmp_path / "lager"
        fake.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            echo "$*" >> {calls}
            answer=$(head -1 {script})
            # The last answer repeats once the script runs out.
            if [ "$(wc -l < {script})" -gt 1 ]; then sed -i.bak 1d {script}; fi
            case "$1:$answer" in
              hello:hello:unlocked) echo "PRD-1 is online and responding!"; exit 0 ;;
              hello:hello:ours) echo "Note: Box 'MASTER' is locked by {OURS}; running read-only." >&2
                                echo "MASTER is online and responding!"; exit 0 ;;
              hello:hello:theirs) echo "Note: Box 'MASTER' is locked by {THEIRS}; running read-only." >&2
                                  echo "MASTER is online and responding!"; exit 0 ;;
              hello:hello:down) echo "Error: Box 'MASTER' did not answer" >&2; exit 1 ;;
              boxes:unlock:ok) echo "Box 'MASTER' is now unlocked"; exit 0 ;;
              boxes:unlock:fail) echo "Error: Box 'MASTER' did not answer" >&2; exit 1 ;;
              *) echo "fake lager: '$*' got answer '$answer' out of order" >&2; exit 99 ;;
            esac
        """))
        fake.chmod(0o755)
        return fake, calls

    return make


def _run(fake, *, repo: "str | None" = REPO, attempts="4"):
    env = {"PATH": "/usr/bin:/bin", "LAGER_BIN": str(fake)}
    if repo is not None:
        env["GITHUB_REPOSITORY"] = repo
    return subprocess.run(["bash", str(HELPER), "MASTER", attempts, "0"],
                          capture_output=True, text=True, env=env, timeout=30)


def _calls(calls):
    return [line.split()[0] + ("-" + line.split()[1] if line.startswith("boxes") else "")
            for line in calls.read_text().splitlines()]


def test_no_lock_means_no_unlock(fake_lager):
    fake, calls = fake_lager("hello:unlocked")
    proc = _run(fake)
    assert proc.returncode == 0
    assert "No box lock held on MASTER." in proc.stdout
    assert _calls(calls) == ["hello"]
    assert "::warning" not in proc.stdout


def test_our_lock_is_released_and_confirmed(fake_lager):
    fake, calls = fake_lager("hello:ours", "unlock:ok", "hello:unlocked")
    proc = _run(fake)
    assert proc.returncode == 0
    assert f"Released the box lock on MASTER held by {OURS}." in proc.stdout
    assert _calls(calls) == ["hello", "boxes-unlock", "hello"]


def test_a_box_that_does_not_answer_is_asked_again(fake_lager):
    """The #639 case: the service is down at first, then comes back."""
    fake, _calls_file = fake_lager("hello:down", "hello:down", "hello:ours", "unlock:ok", "hello:unlocked")
    proc = _run(fake)
    assert proc.returncode == 0
    assert "Released the box lock" in proc.stdout
    assert "::warning" not in proc.stdout


def test_a_box_that_never_answers_is_a_warning_not_a_failure(fake_lager):
    fake, calls = fake_lager("hello:down")
    proc = _run(fake)
    assert proc.returncode == 0, "a failed release must never fail the step"
    assert "::warning title=Box lock state unknown::" in proc.stdout
    assert "lager boxes unlock --box MASTER --force" in proc.stdout
    assert _calls(calls) == ["hello"] * 4


def test_a_lock_that_survives_the_unlock_is_a_warning_naming_it(fake_lager):
    fake, _calls_file = fake_lager("hello:ours", "unlock:fail")
    proc = _run(fake)
    assert proc.returncode == 0
    assert "::warning title=Box lock still held::" in proc.stdout
    assert OURS in proc.stdout
    assert "lager boxes unlock --box MASTER --force" in proc.stdout


def test_an_unlock_on_the_last_attempt_is_still_confirmed(fake_lager):
    fake, _calls_file = fake_lager("hello:ours", "unlock:ok", "hello:unlocked")
    proc = _run(fake, attempts="1")
    assert proc.returncode == 0
    assert "Released the box lock" in proc.stdout
    assert "::warning" not in proc.stdout


def test_someone_elses_lock_is_left_alone(fake_lager):
    """A person working on the bench must not have the box forced from them."""
    fake, calls = fake_lager("hello:theirs")
    proc = _run(fake)
    assert proc.returncode == 0
    assert "boxes-unlock" not in _calls(calls)
    assert "::notice title=Box lock held by someone else::" in proc.stdout
    assert THEIRS in proc.stdout


def test_without_a_repository_no_lock_is_forced(fake_lager):
    """Outside GitHub Actions there is no way to tell a run's lock from a
    person's, so nothing is forced."""
    fake, calls = fake_lager("hello:ours")
    proc = _run(fake, repo=None)
    assert proc.returncode == 0
    assert "boxes-unlock" not in _calls(calls)


def test_no_workflow_releases_a_lock_silently():
    """Every end-of-job and recovery release goes through the helper. The
    silent form is what hid the lock in #639."""
    silent = re.compile(r"lager boxes unlock[^\n]*(2>/dev/null|\|\| true)")
    offenders = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for lineno, line in enumerate(wf.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if silent.search(line):
                offenders.append(f"{wf.name}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
