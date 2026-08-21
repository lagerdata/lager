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
import signal
import subprocess
import time
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


CAPTURE_BLOCK = "ssh stderr capture"


def _extract(topic):
    """Return the shell between the BEGIN/END sentinels naming `topic`.

    Same convention and helper shape as test_deploy_box_image_ref.py. The
    script is ~1600 lines and runs a deployment when sourced, so it cannot be
    sourced in a test; the block is sentinel-delimited precisely so it can be
    lifted out and executed for real. That is the point -- a text assertion
    about `ssh_t` cannot tell an ordered filter from an unordered one, or a
    capture file that is cleaned up from one that leaks.

    The whole block comes across, not just the function. SSH_ERR_FILE and its
    EXIT trap are what make `ssh_t` work and what make it clean up; a harness
    that reconstructed them by hand would be testing the harness.
    """
    begin, end = f"# --- BEGIN {topic}", f"# --- END {topic}"
    body, inside, seen = [], False, False
    for line in DEPLOY_SCRIPT.read_text().splitlines():
        if line.startswith(begin):
            inside, seen = True, True
            continue
        if line.startswith(end):
            inside = False
            continue
        if inside:
            body.append(line)
    assert seen, f"sentinel {begin!r} not found in {DEPLOY_SCRIPT}"
    assert body, f"no shell extracted for {topic!r}"
    return "\n".join(body)


def _harness_prelude():
    """What the deploy script provides the capture block, and nothing else."""
    return (
        "#!/bin/bash\n"
        "set -e\n"
        'SSH_OPTS=""\n'
        'print_error() { echo "[ERROR] $1" >&2; }\n'
    )


def _wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


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
        _harness_prelude()
        + _extract(CAPTURE_BLOCK)
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



@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_ssh_t_cleans_up_when_the_operator_interrupts(tmp_path):
    """Ctrl-C is the case a cleanup line after the ssh call cannot cover.

    This script runs for half an hour across a dozen ssh_t calls, and
    operators do interrupt it. SIGINT to the process group -- exactly what the
    terminal sends -- stops the shell where it stands, so an `rm -f` on the
    next line never runs; a per-call capture file leaked one temp file per
    interrupt, forever, in TMPDIR. The EXIT trap does run (bash runs EXIT
    traps on SIGINT), which is why the file is created once at script scope
    instead of once per call.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "ssh", 'echo "connecting" >&2\nsleep 30')

    harness = tmp_path / "harness.sh"
    harness.write_text(
        _harness_prelude() + _extract(CAPTURE_BLOCK) + '\nssh_t user@box "true"\n'
    )
    harness.chmod(0o755)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["TMPDIR"] = str(scratch)

    proc = subprocess.Popen(
        ["bash", str(harness)], env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        assert _wait_until(lambda: any(scratch.iterdir())), "capture file never appeared"
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert list(scratch.iterdir()) == [], sorted(p.name for p in scratch.iterdir())


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_a_capture_file_that_cannot_be_created_is_explained(tmp_path):
    """The failure has to arrive as a sentence, not as an exit status.

    Allocating the file per call put it inside `ssh_t`, where `set -e` aborts
    the deploy AT THE ASSIGNMENT -- before ssh runs -- and the caller prints
    "Deployment failed! Check the output above for details." above nothing but
    mktemp's own message. That unexplained stop is the entire subject of this
    file; it must not be reintroduced by the fix for it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "ssh", "exit 0")

    harness = tmp_path / "harness.sh"
    harness.write_text(
        _harness_prelude() + _extract(CAPTURE_BLOCK) + '\nssh_t user@box "true"\n'
    )
    harness.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["TMPDIR"] = str(tmp_path / "no-such-directory")
    result = subprocess.run(
        ["bash", str(harness)], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode != 0, result.stdout
    assert "Could not create a temporary file" in result.stdout, result.stdout
    assert "TMPDIR" in result.stdout, result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_a_nul_byte_in_ssh_s_stderr_does_not_swallow_the_error(ssh_t_harness):
    """grep calls a stream with a NUL byte binary and prints "Binary file ...
    matches" INSTEAD of the lines -- destroying the one diagnostic the
    operator needed and replacing it with a path to a file the trap has
    already deleted. `-a` is the whole fix."""
    harness, bin_dir = ssh_t_harness
    binary = bin_dir / "ssh"
    binary.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "{REAL_ERROR}" >&2\n'
        'printf "\\000\\n" >&2\n'
        f'echo "{NOISE}" >&2\n'
        'exit 255\n'
    )
    binary.chmod(0o755)

    _, output = _run_harness(harness, bin_dir)
    assert REAL_ERROR in output, output
    assert "Binary file" not in output, output
    assert NOISE not in output, output


def test_the_controlmaster_trap_also_removes_the_capture_file():
    """bash keeps ONE handler per signal, so the `trap cleanup_ssh EXIT` that
    STEP 1 registers REPLACES the capture file's own trap. If cleanup_ssh does
    not call cleanup_run, every run on a box that needs its own ControlMaster
    leaks the file -- and the leak stays invisible, because the other branch
    (a ControlMaster already in the operator's ssh config) never registers a
    second trap and so still cleans up."""
    code = _uncommented(DEPLOY_SCRIPT)
    body = code[code.index("cleanup_ssh() {"):]
    body = body[:body.index("\n    }")]
    assert "cleanup_run" in body, body


# ---------------------------------------------------------------------------
# The install chain -- built the way the script builds it, then executed
# ---------------------------------------------------------------------------

# The chain is built by bash from a double-quoted string full of `\$` and `\"`.
# That escaping is the part a reader cannot check by eye, and every assertion
# below the divider matches text in the SCRIPT SOURCE, so none of them can see
# it: they would pass just as happily on a string the remote shell would
# refuse. These run the real thing.

REMOTE_SHELLS = ["bash", "sh"]

CHAIN_LINKS = [
    # (what to break, its exit status, the label run_step must print)
    ("apt-get update", 100, "apt-get update"),
    ("apt-get install", 100, "apt-get install -y docker.io docker-compose-v2"),
    ("systemctl daemon-reload", 1, "systemctl daemon-reload"),
    ("systemctl enable", 1, "systemctl enable docker"),
    ("systemctl restart", 5, "systemctl restart docker"),
    ("usermod", 6, "usermod -aG docker lagerdata"),
]


def _remote_install_command(tmp_path):
    """The exact string bash hands the remote shell for the Docker step.

    Not a copy of it. The call site is lifted from the script and run with
    `ssh_t` replaced by a capture function, so bash itself resolves the
    escaping, exactly as it does on a real deploy.
    """
    lines = DEPLOY_SCRIPT.read_text().splitlines()
    start = next(
        i for i, l in enumerate(lines)
        if l.strip().startswith('if ssh_t "${BOX_USER}@${BOX_IP}" "')
    )
    end = next(i for i, l in enumerate(lines[start:], start) if l.strip() == '"; then')
    call_site = "\n".join(lines[start:end + 1])

    out = tmp_path / "remote_cmd"
    builder = tmp_path / "build_remote_cmd.sh"
    builder.write_text(
        "#!/bin/bash\n"
        'BOX_USER="lagerdata"\n'
        'BOX_IP="10.0.0.5"\n'
        'ssh_t() { printf "%s" "$2" > "$OUT"; return 0; }\n'
        + call_site
        + "\n    :\nfi\n"
    )
    builder.chmod(0o755)

    env = dict(os.environ)
    env["OUT"] = str(out)
    result = subprocess.run(
        ["bash", str(builder)], env=env, capture_output=True, text=True,
        timeout=60, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    command = out.read_text()
    assert "run_step()" in command, command
    return command


@pytest.fixture
def box_stubs(tmp_path):
    """A box's worth of stand-ins. Break one by name via FAIL_CMD/FAIL_RC."""
    bin_dir = tmp_path / "box_bin"
    bin_dir.mkdir()
    # Real sudo drops the leading VAR=VAL arguments before exec'ing; so must
    # this one, or `sudo DEBIAN_FRONTEND=... apt-get update` never reaches the
    # apt-get stub and the chain passes for the wrong reason.
    _write_stub(
        bin_dir, "sudo",
        'while [ $# -gt 0 ]; do case "$1" in *=*) shift ;; *) break ;; esac; done\n'
        'exec "$@"',
    )
    for name, key in (("apt-get", '"apt-get $1"'), ("systemctl", '"systemctl $1"'),
                      ("usermod", '"usermod"')):
        _write_stub(
            bin_dir, name,
            f'if [ "${{FAIL_CMD:-}}" = {key} ]; then\n'
            f'    echo "{name}: refusing to $*" >&2\n'
            '    exit "${FAIL_RC:-1}"\n'
            'fi\n'
            'exit 0',
        )
    return bin_dir


def _run_chain(command, bin_dir, shell, fail_cmd=None, fail_rc=None):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    if fail_cmd is not None:
        env["FAIL_CMD"] = fail_cmd
        env["FAIL_RC"] = str(fail_rc)
    else:
        env.pop("FAIL_CMD", None)
    result = subprocess.run(
        [shell, "-c", command], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=60,
        stdin=subprocess.DEVNULL,
    )
    return result.returncode, result.stdout


class TestTheChainActuallyRuns:
    @pytest.mark.parametrize("shell", REMOTE_SHELLS)
    def test_it_succeeds_when_every_command_does(self, tmp_path, box_stubs, shell):
        """`run_step` sits inside an `&&` chain. A wrapper that got the success
        path wrong -- a stray non-zero, a swallowed status -- would fail every
        install on a healthy box."""
        if shutil.which(shell) is None:
            pytest.skip(f"requires {shell}")
        command = _remote_install_command(tmp_path)
        rc, output = _run_chain(command, box_stubs, shell)
        assert rc == 0, output
        assert "STEP FAILED" not in output, output

    @pytest.mark.parametrize("shell", REMOTE_SHELLS)
    @pytest.mark.parametrize("broken,status,label", CHAIN_LINKS)
    def test_each_link_names_itself_and_keeps_its_status(
        self, tmp_path, box_stubs, shell, broken, status, label
    ):
        """Both halves matter. The label is what the operator reads; the exit
        status is what stops the chain -- a wrapper that named the step and
        returned 0 would turn a failed install into a reported success, which
        is worse than the silence it replaced.

        `sh` as well as `bash`: the remote command runs under the box login
        user's shell, which this script does not choose. `run_step` is written
        in POSIX shell for that reason, and nothing here may quietly depend on
        bash.
        """
        if shutil.which(shell) is None:
            pytest.skip(f"requires {shell}")
        command = _remote_install_command(tmp_path)
        rc, output = _run_chain(command, box_stubs, shell,
                                fail_cmd=broken, fail_rc=status)
        assert f"[lager] STEP FAILED: {label} (exit {status})" in output, output
        assert rc == status, output


# ---------------------------------------------------------------------------
# The install chain -- the rest of the contract, pinned in text
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

    def test_the_pointer_to_the_named_step_is_conditional(self):
        """`ssh_t` reports a non-zero status for the SESSION as well as for the
        chain -- a ConnectTimeout, a rejected host key, a broken pipe, a
        ControlMaster that died mid-install -- and two links are deliberately
        unwrapped because a failure there is not fatal. Stating flatly that the
        command "is named above" sends the operator hunting for a line that was
        never printed, which is the same wasted ten minutes this step exists to
        stop causing."""
        code = _uncommented(DEPLOY_SCRIPT)
        tail = code[code.index('print_error "Failed to install Docker"'):]
        tail = tail[:tail.index("exit 1")]
        assert "If the transcript above has" in tail, tail
        assert "If there is no such line" in tail, tail

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

    def test_it_separates_a_box_it_could_not_reach_from_a_disabled_unit(self):
        """`is-enabled` answers 0 for enabled and 1 for disabled or masked;
        ssh answers 255 when it never reached the box at all. Folding 255 in
        with 1 states a fact about the unit from an exit code that never got
        near it -- the same unchecked claim the test above pins, inverted."""
        code = _uncommented(DEPLOY_SCRIPT)
        head = code.index("Ensuring Docker service is enabled")
        block = code[head:head + 1200]
        assert "ENABLE_STATE_RC" in block, block
        assert "-eq 255" in block, block
        assert re.search(r"print_warning .*[Cc]ould not reach the box", block), block
