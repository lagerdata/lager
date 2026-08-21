#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""A failed install step has to say which command failed, and why.

The Docker step of `lager install` ran eight commands as one `&&` chain behind
a single `[ERROR] Failed to install Docker`. Four of them (`daemon-reload`,
`enable`, `restart`, `usermod`) print nothing on success, so a failure in any
of those produced a transcript that simply stopped: no command named, no error
text, nothing to act on. On the box that prompted this, apt visibly succeeded
and the step still failed with no further output.

Three separate things were wrong, and they are tested separately below.

  1. **One message for eight commands.** Fixed by `run_step`, which names the
     command and its exit status on the box's stderr. `ssh -t` merges the
     remote session onto one stream, so that line lands in the transcript
     directly after the failing command's own output.

  2. **`ssh_t`'s stderr filter was asynchronous.** `cmd 2> >(grep -v ... >&2)`
     does not make the shell wait for grep, so ssh's own diagnostics were
     emitted whenever grep next got scheduled. Measured against a stub ssh
     that fails immediately, the pre-fix wrapper printed the real error AFTER
     the caller's generic failure line in 26 of 200 runs; the fixed one in
     0 of 200. `test_ssh_t_*` below runs the real wrapper 40 times and
     requires the order to hold every time, which the old body clears about
     0.3% of the time.

     Worth recording because the issue proposed a stronger reading of this and
     it is not correct: the filter never saw the remote command's stderr at
     all. `-t` allocates a pty, so the remote command's stdout and stderr are
     merged onto the session and arrive on ssh's STDOUT. Only ssh's own local
     messages ever reached this filter, so it could not have dropped the
     failing command's error text. `test_the_filter_only_ever_sees_ssh_s_own_
     stderr` pins the pty behaviour that makes that true.

  3. **The printed recovery instructions were not equivalent to the chain they
     replace.** `systemctl enable docker` was missing from them, so a box
     recovered by hand ran until its next reboot and then came up with no
     docker daemon -- and the re-run skips the whole install block once
     `command -v docker` succeeds. The `enable` in STEP 5 covers that case,
     but it discarded its own result and announced success either way.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_SCRIPT = REPO_ROOT / "cli" / "deployment" / "scripts" / "setup_and_deploy_box.sh"


def _uncommented(path):
    """Script text minus comment lines. The comments here discuss the very
    commands these tests assert the presence or absence of, so matching them
    would let prose satisfy a behavioural assertion."""
    return "\n".join(
        line for line in path.read_text().splitlines()
        if not line.strip().startswith("#")
    )


def _extract_function(name):
    """Lift one top-level shell function out of the deploy script.

    The script is ~1600 lines and runs a deployment when sourced, so it cannot
    be sourced in a test. The function definitions are top-level and brace-
    delimited in column 0, which is enough to slice one out and execute it for
    real -- which is the point: a text assertion about `ssh_t` cannot tell an
    ordered filter from an unordered one.
    """
    lines = DEPLOY_SCRIPT.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i, l in enumerate(lines[start + 1:], start + 1) if l == "}")
    return "\n".join(lines[start:end + 1])


def _write_stub(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# ssh_t -- extracted and executed against a stub ssh
# ---------------------------------------------------------------------------

REAL_ERROR = "ssh: connect to host 10.0.0.5 port 22: Connection refused"
NOISE = "Connection to 10.0.0.5 closed."
GENERIC = "[ERROR] Failed to install Docker"


@pytest.fixture
def ssh_t_harness(tmp_path):
    """A script that calls the real `ssh_t` and then reports failure the way
    the Docker step does -- generic line first, from the caller."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(
        bin_dir,
        "ssh",
        f'echo "{REAL_ERROR}" >&2\n'
        f'echo "{NOISE}" >&2\n'
        'exit "${SSH_RC:-255}"',
    )

    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        'SSH_OPTS=""\n'
        + _extract_function("ssh_t")
        + "\n"
        f'if ssh_t user@box "true"; then echo OK; else echo "{GENERIC}"; exit 1; fi\n'
    )
    harness.chmod(0o755)
    return harness, bin_dir


def _run_harness(harness, bin_dir, ssh_rc=255):
    """stdout and stderr are merged onto ONE pipe on purpose.

    The property under test is the order in which two lines reach the
    operator's terminal, and the terminal is one stream. Capturing them
    separately and concatenating would report a fixed order no matter what the
    script did -- it reads the fix as broken and the bug as fixed.
    """
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SSH_RC"] = str(ssh_rc)
    result = subprocess.run(
        ["bash", str(harness)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    return result, result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_ssh_t_prints_the_real_error_before_the_caller_s_generic_one(ssh_t_harness):
    """40 runs, and the order has to hold in all of them.

    One run proves nothing here: the pre-fix process substitution got the
    order right most of the time and wrong 26 times in 200, which is the worst
    possible failure mode -- it works when you test it by hand and fails on
    the box you cannot reach.
    """
    harness, bin_dir = ssh_t_harness
    misordered = []
    for run in range(40):
        result, output = _run_harness(harness, bin_dir)
        assert result.returncode == 1, output
        assert REAL_ERROR in output, f"run {run}: real error lost entirely\n{output}"
        if output.index(REAL_ERROR) > output.index(GENERIC):
            misordered.append(run)
    assert misordered == [], (
        "ssh's own error must be printed before the caller is told the step "
        f"failed; misordered on runs {misordered}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_ssh_t_still_filters_the_connection_closed_noise(ssh_t_harness):
    """The filter is the whole reason the wrapper exists. Making it ordered
    must not make it a no-op."""
    harness, bin_dir = ssh_t_harness
    _, output = _run_harness(harness, bin_dir)
    assert NOISE not in output, output


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_ssh_t_returns_ssh_s_exit_status_not_the_filter_s(ssh_t_harness):
    """`grep -v` exits 1 when nothing matches, which is the ordinary case for
    a session with a clean stderr. If that status leaks out, every successful
    ssh_t call reads as a failure."""
    harness, bin_dir = ssh_t_harness
    quiet = bin_dir / "ssh"
    quiet.write_text('#!/bin/bash\nexit "${SSH_RC:-0}"\n')
    quiet.chmod(0o755)

    result, output = _run_harness(harness, bin_dir, ssh_rc=0)
    assert result.returncode == 0, output
    assert "OK" in output, output

    result, output = _run_harness(harness, bin_dir, ssh_rc=7)
    assert result.returncode == 1, output


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_ssh_t_leaves_no_temp_files_behind(ssh_t_harness, tmp_path):
    """It captures stderr to a temp file now, once per call, on a code path
    that runs dozens of times per deploy."""
    harness, bin_dir = ssh_t_harness
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["TMPDIR"] = str(scratch)
    env["SSH_RC"] = "255"
    for _ in range(5):
        subprocess.run(
            ["bash", str(harness)], env=env, capture_output=True,
            text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
    assert list(scratch.iterdir()) == [], sorted(p.name for p in scratch.iterdir())


def test_the_filter_only_ever_sees_ssh_s_own_stderr():
    """Pins the pty behaviour the fix's reasoning rests on.

    The issue proposed that the filter was dropping the failing command's
    stderr. It was not, and could not: `-t` allocates a pseudo-terminal, the
    remote command's stdout and stderr are both written to it, and ssh
    forwards the whole session on its own STDOUT. The filter is on ssh's
    stderr. This asserts the merge directly rather than taking it on trust,
    because if it ever stopped being true the fix above would be filtering the
    wrong stream.
    """
    import pty
    import select
    import time

    # Read while the slave is still OPEN, and stop as soon as both markers
    # arrive. Draining after `os.close(slave)` is the obvious shape and works
    # on Linux, where the buffered data outlives the last writer -- but on
    # macOS/BSD the master reports EOF the moment the slave closes and the
    # data is gone, so the assertion below fired on an empty string. Nothing
    # about the property under test is platform-specific; only the draining
    # was. select() keeps the read from blocking now that the writer end is
    # still open.
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["bash", "-c", "echo OUT-LINE; echo ERR-LINE >&2"],
            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        )
        proc.wait(timeout=30)
        seen = b""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.1)[0]:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
            if b"OUT-LINE" in seen and b"ERR-LINE" in seen:
                break
    finally:
        os.close(slave)
        os.close(master)

    text = seen.decode()
    assert "OUT-LINE" in text and "ERR-LINE" in text, repr(text)


# ---------------------------------------------------------------------------
# The install chain -- too large to execute; pin the contract in text
# ---------------------------------------------------------------------------


class TestEveryStepNamesItself:
    def test_run_step_is_defined_and_reports_the_label_and_the_status(self):
        code = _uncommented(DEPLOY_SCRIPT)
        assert "run_step() {" in code
        assert "STEP FAILED" in code, "the failure line the operator greps for"
        assert "step_rc" in code, "the exit status has to travel with the label"

    @pytest.mark.parametrize(
        "label",
        [
            "apt-get update",
            "apt-get install -y docker.io docker-compose-v2",
            "systemctl daemon-reload",
            "systemctl enable docker",
            "systemctl restart docker",
            "usermod -aG docker",
        ],
    )
    def test_each_link_of_the_chain_is_wrapped(self, label):
        """Including -- especially -- the four that are silent on success.
        Those are the ones whose failure produced a transcript that just
        stopped."""
        code = _uncommented(DEPLOY_SCRIPT)
        assert f"run_step '{label}" in code, label

    def test_run_step_returns_the_wrapped_command_s_status(self):
        """It sits inside an `&&` chain. A wrapper that swallowed the status
        would turn a failed install into a reported success, which is worse
        than the bug it fixes."""
        code = _uncommented(DEPLOY_SCRIPT)
        body = code[code.index("run_step() {"):]
        body = body[:body.index("\n            }")]
        assert "return ${step_rc}" in body or "return \\${step_rc}" in body, body

    def test_the_start_limit_shape_is_unchanged(self):
        """This block was already carrying a fix (one service start, socket
        restart demoted to a fallback). Naming the steps must not walk it
        back -- see test_docker_start_limit.py, which owns that contract."""
        code = _uncommented(DEPLOY_SCRIPT)
        assert "sudo systemctl restart docker || {" in code
        assert "systemctl restart docker.socket" in code
        assert code.count("systemctl reset-failed docker.service docker.socket") >= 2


class TestTheFailureMessageIsActionable:
    def test_it_points_at_the_named_step(self):
        code = _uncommented(DEPLOY_SCRIPT)
        assert "STEP FAILED" in code[code.index("Failed to install Docker"):], (
            "the generic line must tell the operator where the specific one is"
        )

    def test_the_daemon_follow_ups_are_offered(self):
        """The issue asks for these by name, and only when the failure is in
        the daemon rather than the packages."""
        code = _uncommented(DEPLOY_SCRIPT)
        tail = code[code.index("Failed to install Docker"):]
        assert "systemctl status docker" in tail
        assert "journalctl -xeu docker.service" in tail
        assert "command -v docker" in tail, (
            "the daemon hint must be conditional on the packages having landed"
        )

    def test_the_recovery_instructions_match_the_chain_they_replace(self):
        """`systemctl enable docker` was missing from them. A box recovered by
        hand then skips the whole install block on the next run (it is gated on
        `command -v docker`) and comes up without a daemon after a reboot."""
        code = _uncommented(DEPLOY_SCRIPT)
        tail = code[code.index("Failed to install Docker"):]
        recovery = tail[:tail.index("exit 1")]
        for command in (
            "apt-get update",
            "apt-get install -y docker.io docker-compose-v2",
            "systemctl daemon-reload",
            "systemctl enable docker",
            "systemctl restart docker",
            "usermod -aG docker",
        ):
            assert command in recovery, f"{command} missing from the recovery path"


class TestTheEnableGrantAndItsVerification:
    def test_enable_is_granted_on_both_systemctl_paths(self):
        """restart and reset-failed both list /bin and /usr/bin, for the
        reason the file states: the grant must match however the box's
        secure_path resolves the binary. enable listed only /bin, so on a box
        that resolves the other one it fell through to a password prompt --
        inside a step that had no way to report one."""
        grants = DEPLOY_SCRIPT.read_text()
        for binary in ("/bin", "/usr/bin"):
            assert f"NOPASSWD: {binary}/systemctl enable docker" in grants, binary

    def test_the_enable_step_checks_before_it_claims_success(self):
        """It is best-effort on purpose -- a box that cannot enable the unit
        still works until it reboots. Best-effort is not licence to announce a
        result the script never read."""
        code = _uncommented(DEPLOY_SCRIPT)
        head = code.index("Ensuring Docker service is enabled")
        block = code[head:head + 900]
        assert "systemctl is-enabled --quiet docker" in block, block
        assert re.search(r"print_warning .*NOT enabled", block), block
