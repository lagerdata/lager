#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""The installer must not trip Docker's systemd start limit.

`docker.service` ships `StartLimitBurst=3` / `StartLimitInterval=60s`. A fresh
provision starts it several times inside that window -- the package postinst,
the installer's pre-flight restart, then the daemon.json restart -- and the
installer used to add a fourth by restarting `docker.socket` immediately before
`docker` (the service declares `Requires=docker.socket`, so the socket restart
bounces the service too). On box JUL-26 that produced four starts in eleven
seconds: systemd refused the fourth and latched the unit into
`failed (start-limit-hit)`, where every further restart -- including the
installer's own retry -- fails instantly without attempting a start.

Docker was healthy throughout; the installer took it down. Two properties keep
it from happening again, and one keeps it diagnosable:

  1. one service start per step, with the socket restart demoted to a fallback
     for the stale-socket failure it was actually added for;
  2. `systemctl reset-failed` before every restart, which clears the counter
     and makes a latched unit recoverable instead of permanently wedged;
  3. `start-limit-hit` reported as itself, not as a bad daemon.json.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "cli" / "deployment" / "scripts"
DNS_SCRIPT = SCRIPTS / "configure_docker_dns.sh"
DEPLOY_SCRIPT = SCRIPTS / "setup_and_deploy_box.sh"

RESET_FAILED = "systemctl reset-failed docker.service docker.socket"


def _uncommented(path):
    """Script text minus comment lines -- comments discuss the very commands
    these tests assert are absent, so matching them would pass on prose."""
    return "\n".join(
        line for line in path.read_text().splitlines()
        if not line.strip().startswith("#")
    )


# ---------------------------------------------------------------------------
# configure_docker_dns.sh -- executed against stubbed sudo/systemctl
# ---------------------------------------------------------------------------


def _write_stub(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(0o755)


@pytest.fixture
def box(tmp_path):
    """A fake box whose `systemctl` records every call it receives."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"

    _write_stub(bin_dir, "sudo", 'exec "$@"')
    _write_stub(
        bin_dir,
        "systemctl",
        'echo "$*" >> "$SYSTEMCTL_LOG"\n'
        'if [ "$1" = "restart" ]; then exit "${RESTART_RC:-0}"; fi\n'
        'if [ "$1" = "is-active" ]; then exit "${RESTART_RC:-0}"; fi\n'
        "exit 0",
    )
    _write_stub(bin_dir, "docker", "exit 0")

    (tmp_path / "resolv.conf").write_text("nameserver 192.168.100.1\n")
    return tmp_path, bin_dir, log


def _run_dns_script(box, restart_rc=0):
    tmp_path, bin_dir, log = box
    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text("{}\n")

    env = dict(os.environ)
    env.update(
        PATH="{}:{}".format(bin_dir, env["PATH"]),
        DAEMON_JSON=str(daemon_json),
        RESOLV_CONF=str(tmp_path / "resolv.conf"),
        STAGED=str(tmp_path / "lager_daemon.json"),
        BACKUP=str(tmp_path / "lager_daemon.json.bak"),
        RESTART_RC=str(restart_rc),
        SYSTEMCTL_LOG=str(log),
    )
    result = subprocess.run(
        ["bash", str(DNS_SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_dns_restart_clears_the_start_limit_first(box):
    result, calls = _run_dns_script(box, restart_rc=0)

    assert result.returncode == 0, result.stderr
    assert "reset-failed docker.service docker.socket" in calls[0], calls
    assert calls[1] == "restart docker", calls


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_the_rollback_restart_also_clears_the_start_limit(box):
    """The path that matters most. A restart that fails is exactly the case
    where the limit is likely already tripped -- if the rollback's own restart
    is refused, the box is left with the old daemon.json AND a dead daemon."""
    result, calls = _run_dns_script(box, restart_rc=1)

    assert result.returncode == 1
    restarts = [i for i, c in enumerate(calls) if c == "restart docker"]
    assert len(restarts) == 2, f"expected a restart and a rollback restart: {calls}"
    for i in restarts:
        assert "reset-failed" in calls[i - 1], f"restart at {i} not preceded by reset: {calls}"


# ---------------------------------------------------------------------------
# setup_and_deploy_box.sh -- too large to execute; pin the contract in text
# ---------------------------------------------------------------------------


class TestDeployScriptRestartSequence:
    def test_socket_is_never_restarted_unconditionally_before_the_service(self):
        # The exact shape that caused the bug: a socket restart chained ahead
        # of the service restart, so both always run.
        code = _uncommented(DEPLOY_SCRIPT)
        offenders = re.findall(
            r"systemctl restart docker\.socket[^\n]*\|\| true; \} && \\", code
        )
        assert offenders == [], (
            "docker.socket restart must be a fallback for a FAILED service "
            f"restart, not a step that always runs: {offenders}"
        )

    def test_socket_restart_survives_as_a_fallback(self):
        # It fixes a different, real failure (a stale socket unit left by a
        # previous docker removal), so demoting it must not delete it.
        code = _uncommented(DEPLOY_SCRIPT)
        assert "sudo systemctl restart docker || {" in code
        assert "systemctl restart docker.socket" in code

    def test_every_restart_site_resets_the_start_limit_first(self):
        code = _uncommented(DEPLOY_SCRIPT)
        # The install step and the daemon-not-running recovery.
        assert code.count(RESET_FAILED) >= 2, (
            f"expected reset-failed at each restart site, found "
            f"{code.count(RESET_FAILED)}"
        )

    def test_no_remote_command_string_breaks_its_own_quoting(self):
        """Same hazard, second location.

        Every `ssh_t "${BOX_USER}@${BOX_IP}" "` opens a DOUBLE-QUOTED string
        that bash expands on the operator's machine before sending it. A
        backtick in there is a command substitution run locally at deploy
        time -- it does not reach the box, it silently deletes the text it
        was wrapping, and whatever it names actually executes. A prose
        comment mentioning `sudo env` in backticks ran `sudo` on the
        operator's own machine and broke the Docker install chain, which is
        how this test came to exist.
        """
        lines = DEPLOY_SCRIPT.read_text().splitlines()
        offenders = []
        i = 0
        while i < len(lines):
            if lines[i].rstrip().endswith('"${BOX_USER}@${BOX_IP}" "'):
                start = i
                i += 1
                while i < len(lines) and not lines[i].strip().startswith('"'):
                    line = lines[i]
                    if "`" in line:
                        offenders.append(f"line {i + 1}: backtick: {line.strip()}")
                    # An unescaped double quote CLOSES the string: everything
                    # after it is re-parsed as outer-shell tokens and the rest
                    # of the chain silently stops being a command. Escaped
                    # quotes (\") are used deliberately by run_step and are
                    # fine.
                    if '"' in line.replace('\\"', ""):
                        offenders.append(f"line {i + 1}: unescaped quote: {line.strip()}")
                    i += 1
                assert i < len(lines), (
                    f"unterminated remote command string opened at line {start + 1}"
                )
            i += 1
        assert offenders == [], (
            "inside a double-quoted remote command string a backtick is a "
            "command substitution evaluated on the OPERATOR'S machine, and an "
            "unescaped double quote closes the string early -- both silently "
            "change what the box is asked to run:\n"
            + "\n".join(offenders)
        )

    def test_that_scan_finds_the_remote_command_strings(self):
        # A scan that matches nothing passes forever.
        lines = DEPLOY_SCRIPT.read_text().splitlines()
        openers = [l for l in lines if l.rstrip().endswith('"${BOX_USER}@${BOX_IP}" "')]
        assert len(openers) >= 2, f"expected several multi-line ssh_t calls, found {len(openers)}"

    def test_the_sudoers_heredoc_contains_no_backticks(self):
        """The sudoers heredoc's delimiter is UNQUOTED (`<< SCRIPT_EOF`), on
        purpose, so ${BOX_USER} expands client-side. That also means any
        backtick in it is executed as a command substitution rather than
        written to the file. A prose comment mentioning `reset-failed` in
        backticks produced `line 446: reset-failed: command not found` on a
        real box and silently emptied that text out of the generated sudoers.
        """
        lines = DEPLOY_SCRIPT.read_text().splitlines()
        start = next(
            i for i, l in enumerate(lines)
            if l.strip() == 'cat > "$TEMP_SCRIPT" << SCRIPT_EOF'
        )
        end = next(
            i for i, l in enumerate(lines[start + 1:], start + 1)
            if l.strip() == "SCRIPT_EOF"
        )
        offenders = [
            (start + 1 + n, l) for n, l in enumerate(lines[start + 1:end]) if "`" in l
        ]
        assert offenders == [], (
            "backticks inside the unquoted heredoc are executed, not written: "
            f"{offenders}"
        )

    def test_reset_failed_is_granted_in_sudoers(self):
        # Without the grant, a non-tty run prompts for a password and the
        # best-effort reset silently does nothing.
        grants = DEPLOY_SCRIPT.read_text()
        for binary in ("/bin", "/usr/bin"):
            assert f"NOPASSWD: {binary}/{RESET_FAILED}" in grants, binary


class TestDaemonDownDiagnosis:
    """`start-limit-hit` was reported as a malformed daemon.json, because
    writing daemon.json is the step before the restart that fails. The two are
    unrelated, and the wrong hint cost a debugging session."""

    def test_start_limit_hit_is_named_and_given_its_own_remedy(self):
        code = _uncommented(DEPLOY_SCRIPT)
        assert "systemctl show docker -p Result" in code, (
            "ask the unit why it is down before guessing"
        )
        assert 'start-limit-hit' in code
        # The remedy has to be the one that actually works: a plain restart is
        # refused while the unit is latched.
        assert f"sudo {RESET_FAILED}" in code

    def test_the_daemon_json_guess_is_now_conditional(self):
        code = _uncommented(DEPLOY_SCRIPT)
        hint = "A daemon that refuses to start usually has a bad /etc/docker/daemon.json."
        assert hint in code, "still the right hint for every OTHER failure"
        start_limit_branch = code.index('"start-limit-hit"')
        assert start_limit_branch < code.index(hint), (
            "the daemon.json hint must sit in the else-branch, after the "
            "start-limit-hit case has had its chance"
        )
