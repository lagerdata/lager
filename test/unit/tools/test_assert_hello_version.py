# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`tools/assert_hello_version.sh` must fail when the box runs other code.

The lifecycle job's "Verify box healthy" steps ran a bare `lager hello`, which
passes for any box that answers, so a phase that left the box on the wrong
version still went green. These tests drive the real script against a fake
`lager` that prints a given `hello` answer.
"""

import pathlib
import shutil
import subprocess
import textwrap

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
HELPER = REPO_ROOT / "tools" / "assert_hello_version.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "update-regression.yml"

SHA = "0123456789abcdef0123456789abcdef01234567"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def fake_lager(tmp_path):
    """A `lager` stand-in whose `hello` prints `version_line` and exits `rc`."""

    def make(version_line, rc=0):
        fake = tmp_path / "lager"
        fake.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            [ "$1" = hello ] || {{ echo "fake lager: unexpected '$*'" >&2; exit 99; }}
            printf '\\nBox: MASTER\\nIP: 192.0.2.10\\n%b\\n\\nMASTER is online and responding!\\n' '{version_line}'
            exit {rc}
        """))
        fake.chmod(0o755)
        return fake

    return make


def _run(fake, *args):
    env = {"PATH": "/usr/bin:/bin", "LAGER_BIN": str(fake)}
    return subprocess.run(["bash", str(HELPER), "MASTER", *args],
                          capture_output=True, text=True, env=env, timeout=30)


def test_the_expected_release_passes(fake_lager):
    proc = _run(fake_lager("Version: 0.59.0 (release)"), "0.59.0", "(release)")
    assert proc.returncode == 0, proc.stdout
    assert "Version: 0.59.0 (release)" in proc.stdout
    assert "::error" not in proc.stdout


def test_another_version_fails(fake_lager):
    proc = _run(fake_lager("Version: 0.58.0 (release)"), "0.59.0")
    assert proc.returncode == 1
    assert "::error title=Wrong box version::" in proc.stdout
    assert "'0.58.0'" in proc.stdout


def test_a_version_that_only_starts_the_same_fails(fake_lager):
    proc = _run(fake_lager("Version: 0.59.01"), "0.59.0")
    assert proc.returncode == 1


def test_a_commit_build_passes_on_its_sha(fake_lager):
    # `hello` colours a non-release ref yellow.
    line = f"Version: 0.59.0 \\033[33m({SHA}@{SHA} -- not a release build)\\033[0m"
    proc = _run(fake_lager(line), "0.59.0", SHA)
    assert proc.returncode == 0, proc.stdout


def test_the_right_version_on_the_wrong_commit_fails(fake_lager):
    other = "f" * 40
    line = f"Version: 0.59.0 ({other}@{other} -- not a release build)"
    proc = _run(fake_lager(line), "0.59.0", SHA)
    assert proc.returncode == 1
    assert "::error title=Wrong box ref::" in proc.stdout


def test_a_release_check_fails_on_a_branch_build(fake_lager):
    line = f"Version: 0.59.0 ({SHA}@{SHA} -- not a release build)"
    assert _run(fake_lager(line), "0.59.0", "(release)").returncode == 1


def test_an_unknown_version_fails(fake_lager):
    proc = _run(fake_lager("Version: Unknown"), "0.59.0")
    assert proc.returncode == 1
    assert "'Unknown'" in proc.stdout


def test_a_box_that_does_not_answer_fails(fake_lager):
    proc = _run(fake_lager("Error: Timed out connecting to the box", rc=1), "0.59.0")
    assert proc.returncode == 1
    assert "::error title=lager hello failed::" in proc.stdout


def test_every_lifecycle_health_check_asserts_the_version():
    """Each "Verify box healthy" step must run the helper, not a bare hello."""
    text = WORKFLOW.read_text()
    steps = text.split("\n      - name: ")
    verify = [s for s in steps if s.startswith("Verify box healthy")]
    assert len(verify) >= 4, "the lifecycle job lost a health check"
    for step in verify:
        assert "tools/assert_hello_version.sh" in step, step.splitlines()[0]
