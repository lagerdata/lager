# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Cleanup gets as long as it needs, as long as it is still getting somewhere.

A fixed cleanup budget has to be guessed on behalf of every user's teardown,
and any guess is wrong for someone -- ours needs ~2.4s, a bench with six
LabJacks and three hubs could need ten times that, and a number large enough
for them lets a wedged script hold a shared box for that long. These tests pin
the replacement: an *idle* budget, extended while the script is demonstrably
still executing code, with a ceiling for the case that cannot be distinguished
from outside.

Progress is driven explicitly here rather than read off /proc. The real
implementation is Linux-only and the unit suite runs on macOS, where it
degrades to a plain fixed wait -- so a test that used it would either be
vacuous or untestable. The /proc reader is covered on hardware; what these
cover is the decision logic built on top of it.
"""

import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box'))

from lager.exec import process as process_mod  # noqa: E402
from lager.exec import quiesce  # noqa: E402
from lager.exec.process import terminate_process, wait_for_cleanup  # noqa: E402


class WatchdogTestCase(unittest.TestCase):
    def setUp(self):
        self.children = []
        self.addCleanup(self.reap)

    def reap(self):
        for proc in self.children:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def spawn(self, body):
        proc = subprocess.Popen(
            [sys.executable, '-c', body],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.children.append(proc)
        return proc

    def progress(self, sequence):
        """Patch progress_snapshot to yield ``sequence``, repeating the last."""
        state = {'i': 0}

        def snapshot(pid):
            i = min(state['i'], len(sequence) - 1)
            state['i'] += 1
            return sequence[i]

        return mock.patch.object(quiesce, 'progress_snapshot', snapshot)


class IdleBudgetTests(WatchdogTestCase):
    def test_a_teardown_that_keeps_working_runs_past_the_idle_budget(self):
        """The whole point: 6s of cleanup completes under a 1s idle budget."""
        proc = self.spawn(
            'import time\n'
            'try:\n'
            '    time.sleep(60)\n'
            'except KeyboardInterrupt:\n'
            '    time.sleep(3.0)\n'      # stands in for a real teardown
        )
        time.sleep(0.3)

        # Progress ticks forward on every sample, i.e. the script is busy.
        counter = {'n': 0}

        def busy(pid):
            counter['n'] += 1
            return (counter['n'], frozenset({pid}))

        started = time.monotonic()
        with mock.patch.object(quiesce, 'progress_snapshot', busy):
            returncode = terminate_process(proc, cleanup_grace_s=1.0)
        elapsed = time.monotonic() - started

        self.assertGreater(
            elapsed, 2.5,
            'cleanup was cut off at the idle budget despite making progress',
        )
        self.assertNotEqual(
            returncode, -1,
            'the process was killed rather than allowed to exit on its own',
        )

    def test_a_wedged_teardown_is_cut_off_at_the_idle_budget(self):
        """No progress means no amount of waiting would have helped."""
        proc = self.spawn(
            'import signal, time\n'
            'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            'time.sleep(60)\n'
        )
        time.sleep(0.3)

        started = time.monotonic()
        with self.progress([(1, frozenset({proc.pid}))]):  # frozen: no progress
            returncode = terminate_process(proc, cleanup_grace_s=0.5)
        elapsed = time.monotonic() - started

        self.assertEqual(returncode, -1, 'the wedged process was not killed')
        self.assertLess(
            elapsed, 6.0,
            'a process making no progress held the bench far longer than its '
            'idle budget',
        )

    def test_the_ceiling_stops_a_script_that_ignored_the_interrupt(self):
        """A script that carried on with its test looks exactly like a busy
        teardown from out here. Only the ceiling separates them."""
        proc = self.spawn(
            'import signal, time\n'
            'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            'time.sleep(60)\n'
        )
        time.sleep(0.3)

        counter = {'n': 0}

        def busy(pid):
            counter['n'] += 1
            return (counter['n'], frozenset({pid}))

        started = time.monotonic()
        with mock.patch.object(quiesce, 'progress_snapshot', busy), \
                mock.patch.object(process_mod, 'CLEANUP_MAX_S', 1.0):
            with self.assertLogs('lager.exec.process', level='WARNING') as logs:
                returncode = terminate_process(proc, cleanup_grace_s=0.5)
        elapsed = time.monotonic() - started

        self.assertEqual(returncode, -1)
        self.assertLess(elapsed, 6.0, 'the ceiling did not bound a busy script')
        self.assertIn('ceiling', '\n'.join(logs.output))

    def test_without_progress_information_it_behaves_exactly_as_before(self):
        """No /proc (macOS, and any future non-Linux host) must not regress
        into either an instant kill or an unbounded wait."""
        proc = self.spawn(
            'import signal, time\n'
            'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            'time.sleep(60)\n'
        )
        time.sleep(0.3)

        started = time.monotonic()
        with mock.patch.object(quiesce, 'progress_snapshot', lambda pid: None):
            returncode = terminate_process(proc, cleanup_grace_s=0.5)
        elapsed = time.monotonic() - started

        self.assertEqual(returncode, -1)
        self.assertGreater(elapsed, 0.5, 'did not honour the budget at all')
        self.assertLess(elapsed, 6.0, 'waited well past the budget')


class WaitForCleanupTests(WatchdogTestCase):
    def test_returns_the_return_code_when_the_process_exits(self):
        proc = self.spawn('import sys; sys.exit(7)')
        self.assertEqual(wait_for_cleanup(proc, idle_s=5.0), 7)

    def test_returns_none_when_the_process_outlives_the_budget(self):
        proc = self.spawn('import time; time.sleep(60)')
        with mock.patch.object(quiesce, 'progress_snapshot', lambda pid: None):
            self.assertIsNone(wait_for_cleanup(proc, idle_s=0.3))


@unittest.skipUnless(os.path.isdir('/proc'), 'requires /proc (Linux)')
class ProcReaderTests(WatchdogTestCase):
    """The Linux-only half. Skipped on the macOS dev box; runs in CI/on-box."""

    def test_a_busy_process_moves_its_snapshot(self):
        proc = self.spawn('x = 0\nwhile True:\n    x += 1\n')
        time.sleep(0.2)
        first = quiesce.progress_snapshot(proc.pid)
        time.sleep(0.4)
        second = quiesce.progress_snapshot(proc.pid)
        self.assertIsNotNone(first)
        self.assertNotEqual(first, second)

    def test_a_sleeping_process_does_not(self):
        """One long sleep parks the process once and then nothing happens, so
        neither counter moves. This is the case the watchdog must still cut
        off, and it is what stops the context-switch signal from simply
        declaring everything busy."""
        proc = self.spawn('import time; time.sleep(60)')
        time.sleep(0.3)
        first = quiesce.progress_snapshot(proc.pid)
        time.sleep(0.5)
        self.assertEqual(first, quiesce.progress_snapshot(proc.pid))

    def test_a_process_blocked_on_io_shows_progress(self):
        """The defect the context-switch signal exists for.

        A teardown talking to an instrument is blocked, not computing. On
        hardware one Acroname hub round trip takes ~2.2s and accrues about one
        10ms tick, so CPU time alone reported a working teardown as idle for up
        to 4.17s -- past the cleanup budget, which cut it off mid-cleanup.
        Repeated short blocking stands in for that here.
        """
        proc = self.spawn(
            'import time\n'
            'while True:\n'
            '    time.sleep(0.05)\n'
        )
        time.sleep(0.3)

        first_switches = quiesce.ctxt_switches(proc.pid)
        first_snapshot = quiesce.progress_snapshot(proc.pid)
        time.sleep(0.5)
        second_switches = quiesce.ctxt_switches(proc.pid)
        second_snapshot = quiesce.progress_snapshot(proc.pid)

        self.assertGreater(
            second_switches, first_switches,
            'a process blocked on I/O registered no progress at all, which is '
            'what truncated cleanup mid-teardown on hardware',
        )
        self.assertNotEqual(
            first_snapshot, second_snapshot,
            'the switch counter moved but the snapshot did not, so the '
            'watchdog would still not see the progress',
        )

    def test_ctxt_switches_is_zero_for_a_dead_pid(self):
        proc = self.spawn('import sys; sys.exit(0)')
        proc.wait(timeout=5)
        self.assertEqual(quiesce.ctxt_switches(proc.pid), 0)

    def test_the_tree_includes_children(self):
        proc = self.spawn(
            'import subprocess, sys, time\n'
            'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
            'time.sleep(30)\n'
        )
        time.sleep(0.6)
        self.assertGreaterEqual(
            len(quiesce.process_tree(proc.pid)), 2,
            'a grandchild doing the cleanup work would be invisible',
        )


if __name__ == '__main__':
    unittest.main()
