# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The `lager python --timeout` deadline, and whether it can actually be met.

`--timeout` was reaching the box and being applied -- `/usr/bin/timeout N` --
and still never firing. GNU timeout sends SIGTERM at the deadline and nothing
more, so a script that does not return from SIGTERM does not stop: one blocked
in an uninterruptible call (a pyvisa/libusb/serial read, which is the normal
case on a box) or one that installs its own handler. `--timeout 3` against a
30-second sleep was measured still running 17 minutes later, ended by a CI step
timeout rather than by the timeout it was given.

Two tests here spawn REAL children under a real /usr/bin/timeout, because that
is the only thing that answers the question. Asserting on a mock would confirm
we build the argv we decided to build -- which is what the argv tests below
already cover -- and say nothing about whether the process dies. The pairing is
deliberate: the same script is run under the old argv and the new one, so the
test states the defect and the fix rather than only the fix.

Note the exit code the fix produces, 137, is `SIGKILL_EXIT_CODE` in
cli/core/utils.py, which `_do_exit` has always rendered as "Script forcibly
killed due to timeout." Nothing could reach that message before, because
nothing escalated to SIGKILL.
"""

import logging
import os
import platform
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from lager.exec.process import CLEANUP_GRACE_S
from lager.python.executor import MAX_TIMEOUT, _wrap_with_timeout

TIMEOUT_BIN = '/usr/bin/timeout'

# The real-process tests need GNU coreutils at the path the box uses. The box is
# Linux; a dev machine may not be, and macOS has no /usr/bin/timeout at all.
needs_real_timeout = pytest.mark.skipif(
    platform.system() != 'Linux' or not os.path.exists(TIMEOUT_BIN),
    reason=f'needs GNU coreutils at {TIMEOUT_BIN} (the box path)',
)

IGNORES_SIGTERM = textwrap.dedent('''
    import signal, sys, time
    signal.signal(signal.SIGTERM, lambda *a: None)
    sys.stdout.write("up\\n"); sys.stdout.flush()
    time.sleep(300)
''')


class TestArgv:
    """What gets executed, for each shape of request."""

    def test_deadline_carries_a_kill_after(self):
        argv = _wrap_with_timeout(['python3', 's.py'], 3, False)
        assert argv[0] == TIMEOUT_BIN
        assert '--kill-after' in argv, (
            'without --kill-after the deadline is unenforceable against a '
            'script that does not die on SIGTERM'
        )

    def test_grace_matches_the_escalation_used_elsewhere(self):
        argv = _wrap_with_timeout(['python3', 's.py'], 3, False)
        assert argv[argv.index('--kill-after') + 1] == str(CLEANUP_GRACE_S)

    def test_the_duration_is_the_requested_one(self):
        argv = _wrap_with_timeout(['python3', 's.py'], 7, False)
        assert argv[argv.index('--kill-after') + 2] == '7'

    def test_command_is_preserved_verbatim_and_last(self):
        cmd = ['python3', '/tmp/x/main.py', '--flag', 'a b']
        assert _wrap_with_timeout(list(cmd), 5, False)[-len(cmd):] == cmd

    def test_zero_means_no_limit(self):
        # coreutils: "A duration of 0 disables the associated timeout", so the
        # wrapper is inert on the default path. Verified against coreutils 9.1.
        argv = _wrap_with_timeout(['python3', 's.py'], 0, False)
        assert argv[argv.index('--kill-after') + 2] == '0'

    def test_above_the_ceiling_is_capped(self):
        argv = _wrap_with_timeout(['python3', 's.py'], MAX_TIMEOUT + 300, False)
        assert argv[argv.index('--kill-after') + 2] == str(MAX_TIMEOUT)

    def test_capping_is_not_silent(self, caplog):
        # A silent min() reads as the timeout firing early rather than as a
        # ceiling being applied.
        requested = MAX_TIMEOUT + 300
        with caplog.at_level(logging.WARNING, logger='lager.python.executor'):
            _wrap_with_timeout(['python3', 's.py'], requested, False)
        messages = [r.getMessage() for r in caplog.records]
        assert any(str(requested) in m and str(MAX_TIMEOUT) in m
                   for m in messages), messages

    def test_detached_is_not_wrapped(self):
        cmd = ['python3', 's.py']
        assert _wrap_with_timeout(list(cmd), 3, True) == cmd

    def test_detached_says_the_timeout_is_being_dropped(self, caplog):
        with caplog.at_level(logging.WARNING, logger='lager.python.executor'):
            _wrap_with_timeout(['python3', 's.py'], 3, True)
        assert 'ignored' in caplog.text.lower(), caplog.text


@needs_real_timeout
class TestARealScriptThatIgnoresSigterm:
    """The defect and the fix, against a real process."""

    DEADLINE = 2

    def _run(self, argv_builder, tmp_path, bound):
        script = tmp_path / 'ignores.py'
        script.write_text(IGNORES_SIGTERM)
        argv = argv_builder([sys.executable, str(script)])
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=bound)
            return proc.returncode, time.monotonic() - started
        except subprocess.TimeoutExpired:
            # Nothing killed it. Do it ourselves so the suite cannot leak a
            # 300-second sleeper into the rest of the run.
            subprocess.run(['pkill', '-9', '-f', str(script)], check=False)
            return None, time.monotonic() - started

    def test_sigterm_alone_does_not_stop_it(self, tmp_path):
        """The bug: the old argv, which sent SIGTERM and nothing else."""
        rc, elapsed = self._run(
            lambda cmd: [TIMEOUT_BIN, str(self.DEADLINE)] + cmd,
            tmp_path, bound=self.DEADLINE + 8,
        )
        assert rc is None, (
            f'expected the script to survive its own deadline (rc={rc} after '
            f'{elapsed:.1f}s) -- if this now stops on its own, GNU timeout '
            f'changed and this test no longer describes the defect'
        )

    def test_kill_after_stops_it(self, tmp_path):
        """The fix: same script, the argv the executor now builds."""
        rc, elapsed = self._run(
            lambda cmd: _wrap_with_timeout(cmd, self.DEADLINE, False),
            tmp_path, bound=self.DEADLINE + CLEANUP_GRACE_S + 15,
        )
        # -9, NOT 137. This layer reports Python's returncode, which is
        # negative for a signal death; 137 is the shell's 128+N convention, and
        # cli/core/utils.py's normalize_exit_code is what maps one to the other
        # (pinned in test/unit/cli/test_python_exit_codes.py). Asserting 137
        # here asserted something the box never produces -- which went unnoticed
        # because this class is gated on GNU coreutils and skips on the macOS
        # it was written on.
        assert rc == -signal.SIGKILL, (
            f'expected death by SIGKILL ({-signal.SIGKILL}), got {rc}'
        )
        assert elapsed >= self.DEADLINE, (
            f'died at {elapsed:.1f}s, before its {self.DEADLINE}s deadline'
        )
        assert elapsed < self.DEADLINE + CLEANUP_GRACE_S + 10, (
            f'took {elapsed:.1f}s; the grace window is {CLEANUP_GRACE_S}s'
        )

    def test_a_well_behaved_script_still_exits_on_sigterm(self, tmp_path):
        """The grace must not turn every timeout into a SIGKILL: a script that
        honours SIGTERM should still exit 124, so its cleanup is not cut off."""
        script = tmp_path / 'polite.py'
        script.write_text('import time\ntime.sleep(300)\n')
        argv = _wrap_with_timeout([sys.executable, str(script)], 1, False)
        proc = subprocess.run(argv, capture_output=True, timeout=30)
        assert proc.returncode == 124, (
            f'expected SIGTERM_EXIT_CODE 124, got {proc.returncode}'
        )

class TestTheCeilingFitsInsideTheClientsReadTimeout:
    """A deadline the client gives up on first is not a deadline.

    The box can now hold a /python response for MAX_TIMEOUT + CLEANUP_GRACE_S
    before the job is force-killed and the response completes. If the CLI's
    HTTP read timeout is shorter than that, the client raises a connection
    error instead of reporting the timeout, and `_do_exit`'s "forcibly killed"
    message is unreachable again -- for a different reason.

    The two numbers live either side of the wheel boundary (box/ does not ship
    in lager-cli), so this reads the literal out of the source the way
    cli/tests/test_host_cli.py pins its shell mirror.
    """

    def _client_read_timeout(self):
        import re
        from pathlib import Path
        import cli.context.session as session_mod
        text = Path(session_mod.__file__).read_text()
        # The run_python POST: `timeout=(connect, read)`.
        block = text[text.index('def run_python', text.index('class DirectHTTPSession')):]
        match = re.search(r'timeout=\(\s*(\d+)\s*,\s*(\d+)\s*\)', block)
        assert match, 'could not find the read timeout for the /python POST'
        return int(match.group(2))

    def test_the_box_finishes_before_the_client_stops_listening(self):
        read_timeout = self._client_read_timeout()
        worst_case = MAX_TIMEOUT + CLEANUP_GRACE_S
        assert worst_case < read_timeout, (
            f'the box can hold the response for {worst_case}s '
            f'(MAX_TIMEOUT {MAX_TIMEOUT} + CLEANUP_GRACE_S {CLEANUP_GRACE_S}) '
            f'but the CLI stops reading at {read_timeout}s, so a job at the '
            f'ceiling reports a connection error rather than a timeout. Raise '
            f'the read timeout in cli/context/session.py with it.'
        )
