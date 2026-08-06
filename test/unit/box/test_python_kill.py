# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Signalling and reaping behind POST /python/kill.

These tests spawn REAL child processes rather than mocking os.kill. The two
defects they cover are both about what actually happens to a PID -- whether it
receives a signal at all, and how long the handler holds the request while it
waits -- and asserting on a mock would only confirm that we called a function
we wrote.

The scenario: a `lager python` job is several processes, all carrying
LAGER_PROCESS_ID -- the /usr/bin/timeout wrapper, the python3 script it wraps,
and anything the script spawned. Stopping the job means stopping all of them.

Three things used to go wrong. `_kill_by_proc_id` signalled whichever match
glob('/proc/*/environ') returned first and then returned, and glob yields
readdir order, so a job whose script had spawned a child could have that child
killed -- and reported as a successful kill -- while the script itself ran on,
still driving the hardware. Escalation was serial per PID: each match got its
own 3s grace window before the next was touched, so a three-process job could
hold the kill request open for 9s while the client's POST timed out underneath
it. And every match was signalled directly, which sounds harmless but is not:
the /usr/bin/timeout wrapper forwards to the process group it shares with the
script, so signalling both delivers the interrupt twice, and the second one
lands inside the `finally` unwinding from the first and truncates the cleanup.
Measured against GNU coreutils 9.7: wrapper+script gives 2 deliveries and
cleanup dies after 1 of 4 steps; either alone gives 1 and it completes.
"""

import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.python import executor  # noqa: E402


# Short enough to keep the suite quick, long enough that a serial-per-PID
# regression shows up as a multiple of it rather than as noise.
TEST_GRACE_S = 0.6


def _spawn(source, **kwargs):
    """Start a child and block until it reports readiness.

    The handshake matters: these tests signal the child immediately, and
    without it we would race the interpreter's startup and could deliver
    SIGTERM before the child had installed its handler.

    Returns the Popen plus whatever the child printed after 'ready', which the
    process-group cases use to learn a grandchild's PID.
    """
    proc = subprocess.Popen(
        [sys.executable, '-c', source],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    line = proc.stdout.readline().split()
    assert line and line[0] == b'ready', line
    return proc, [int(x) for x in line[1:]]


_READY = 'import sys, time\nsys.stdout.write("ready\\n"); sys.stdout.flush()\n'

# A group leader with one child inside its group, printing the child's PID.
# Stands in for the /usr/bin/timeout wrapper and the script it wraps.
_LEADER_WITH_CHILD = (
    'import subprocess, sys, time\n'
    'kid = subprocess.Popen([sys.executable, "-c",\n'
    '                        "import time\\nwhile True: time.sleep(1)"])\n'
    'sys.stdout.write("ready %d\\n" % kid.pid); sys.stdout.flush()\n'
    'while True: time.sleep(1)\n'
)

# Default SIGTERM disposition: dies as soon as it is signalled.
_DIES_ON_SIGTERM = _READY + 'time.sleep(300)\n'

# Swallows SIGTERM, so only the SIGKILL escalation can stop it. This is the
# shape of a script wedged in cleanup, which is what the grace window exists
# for in the first place.
_IGNORES_SIGTERM = (
    'import signal\nsignal.signal(signal.SIGTERM, lambda *a: None)\n'
    + _READY
    + 'while True: time.sleep(1)\n'
)


class KillTestCase(unittest.TestCase):
    """Spawns children and guarantees they are gone when the test ends."""

    def setUp(self):
        self.procs = []
        self.stray_pids = []
        patcher = mock.patch.object(executor, 'KILL_GRACE_S', TEST_GRACE_S)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._reap_all)

    def spawn(self, source, count=1, **kwargs):
        procs = [_spawn(source, **kwargs)[0] for _ in range(count)]
        self.procs.extend(procs)
        return procs

    def spawn_group_leader(self, source):
        """Start a process that leads its own group, plus a child inside it.

        ``start_new_session=True`` makes the parent a session and process-group
        leader, so its PGID equals its PID and the grandchild inherits it --
        the same shape /usr/bin/timeout creates around a script on the box.
        """
        proc, extra = _spawn(source, start_new_session=True)
        self.procs.append(proc)
        self.stray_pids.extend(extra)
        return proc, extra[0]

    def _reap_all(self):
        for pid in self.stray_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()

    def assertExited(self, proc, timeout=5.0):
        """Assert the child is gone, and reap it so it is not left a zombie."""
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.fail(f'PID {proc.pid} survived; it was never signalled')


class SignalAndReapTests(KillTestCase):

    def test_signals_every_pid(self):
        procs = self.spawn(_DIES_ON_SIGTERM, count=3)

        executor._signal_and_reap([p.pid for p in procs], signal.SIGTERM)

        for proc in procs:
            self.assertExited(proc)

    def test_escalates_to_sigkill_when_the_signal_is_ignored(self):
        procs = self.spawn(_IGNORES_SIGTERM, count=2)

        executor._signal_and_reap([p.pid for p in procs], signal.SIGTERM)

        for proc in procs:
            self.assertExited(proc)
            self.assertEqual(proc.returncode, -signal.SIGKILL)

    def test_grace_window_is_shared_not_serial(self):
        """Three wedged processes must cost one grace window, not three.

        This is the bound that keeps the kill RPC from outliving the client's
        POST timeout. Asserting on wall clock is the only way to see it: every
        other observable outcome is identical either way.
        """
        procs = self.spawn(_IGNORES_SIGTERM, count=3)

        start = time.monotonic()
        executor._signal_and_reap([p.pid for p in procs], signal.SIGTERM)
        elapsed = time.monotonic() - start

        for proc in procs:
            self.assertExited(proc)
        self.assertLess(elapsed, TEST_GRACE_S * 2)

    def test_sigkill_does_not_wait(self):
        """SIGKILL has nothing to escalate to, so it must not burn the grace."""
        procs = self.spawn(_IGNORES_SIGTERM, count=2)

        start = time.monotonic()
        signalled = executor._signal_and_reap([p.pid for p in procs], signal.SIGKILL)
        elapsed = time.monotonic() - start

        self.assertEqual(signalled, 2)
        self.assertLess(elapsed, TEST_GRACE_S)
        for proc in procs:
            self.assertExited(proc)

    def test_a_dead_pid_does_not_stop_the_rest(self):
        """One stale PID in the set must not abort the sweep.

        PIDs are collected from a /proc scan, so by the time we signal them a
        short-lived grandchild may already be gone.
        """
        alive, doomed = self.spawn(_DIES_ON_SIGTERM, count=2)
        doomed.kill()
        doomed.wait()

        signalled = executor._signal_and_reap(
            [doomed.pid, alive.pid], signal.SIGTERM
        )

        self.assertEqual(signalled, 1)
        self.assertExited(alive)


class SignalTargetTests(KillTestCase):
    """One signal per process group, so an interrupt is delivered exactly once.

    The regression these pin is subtle because the extra signal looks free:
    every PID is genuinely part of the job, and signalling it succeeds. What
    it costs is the script's cleanup, which the duplicate interrupts.
    """

    def test_a_member_covered_by_its_leader_is_not_signalled_again(self):
        leader, child = self.spawn_group_leader(_LEADER_WITH_CHILD)

        targets = executor._signal_targets([leader.pid, child], signal.SIGTERM)

        self.assertEqual(targets, [leader.pid])

    def test_sigkill_names_every_pid(self):
        """Nothing forwards SIGKILL, and there is no cleanup to protect."""
        leader, child = self.spawn_group_leader(_LEADER_WITH_CHILD)

        targets = executor._signal_targets([leader.pid, child], signal.SIGKILL)

        self.assertEqual(sorted(targets), sorted([leader.pid, child]))

    def test_a_member_whose_leader_is_not_ours_is_signalled_directly(self):
        """A grandchild that called setsid() has nobody to forward to it."""
        _, child = self.spawn_group_leader(_LEADER_WITH_CHILD)

        targets = executor._signal_targets([child], signal.SIGTERM)

        self.assertEqual(targets, [child])

    def test_a_dead_leader_does_not_swallow_its_members(self):
        """Relying on forwarding is only safe while the forwarder is alive."""
        leader, child = self.spawn_group_leader(_LEADER_WITH_CHILD)
        leader.kill()
        leader.wait()

        targets = executor._signal_targets([leader.pid, child], signal.SIGTERM)

        self.assertEqual(targets, [child])

    def test_the_wait_still_covers_members_that_were_not_signalled(self):
        """A member reached by forwarding must still be seen to exit.

        Otherwise the RPC returns while part of the job is still running --
        the original defect in a new disguise.
        """
        leader, child = self.spawn_group_leader(_LEADER_WITH_CHILD)

        with mock.patch.object(
            executor, '_pid_is_alive', wraps=executor._pid_is_alive
        ) as alive:
            executor._signal_and_reap([leader.pid, child], signal.SIGTERM)

        polled = {call.args[0] for call in alive.call_args_list}
        self.assertIn(child, polled)


class KillByProcIdTests(KillTestCase):

    PROC_ID = '0d1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8'

    def test_kills_every_process_in_the_job(self):
        """The regression: only the first match used to be signalled.

        Ordering is not stable -- glob returns readdir order -- so the old
        code's behaviour depended on which of the job's processes the kernel
        happened to list first.
        """
        procs = self.spawn(_DIES_ON_SIGTERM, count=3)
        pids = [p.pid for p in procs]

        with mock.patch.object(executor, '_scan_lager_pids', return_value=pids):
            executor._kill_by_proc_id(signal.SIGTERM, self.PROC_ID)

        for proc in procs:
            self.assertExited(proc)

    def test_accepts_a_bytes_process_id(self):
        procs = self.spawn(_DIES_ON_SIGTERM, count=1)

        with mock.patch.object(
            executor, '_scan_lager_pids', return_value=[procs[0].pid]
        ) as scan:
            executor._kill_by_proc_id(signal.SIGTERM, self.PROC_ID.encode())

        scan.assert_called_once_with(
            f'LAGER_PROCESS_ID={self.PROC_ID}'.encode()
        )
        self.assertExited(procs[0])

    def test_no_match_is_reported_not_raised(self):
        with mock.patch.object(executor, '_scan_lager_pids', return_value=[]):
            with self.assertLogs(executor.logger, level='WARNING') as logs:
                executor._kill_by_proc_id(signal.SIGTERM, self.PROC_ID)

        self.assertIn(self.PROC_ID, '\n'.join(logs.output))


class KillAllTests(KillTestCase):

    def test_kills_every_matching_process(self):
        procs = self.spawn(_DIES_ON_SIGTERM, count=3)
        pids = [p.pid for p in procs]

        with mock.patch.object(executor, '_scan_lager_pids', return_value=pids) as scan:
            executor._kill_all_lager_processes(signal.SIGTERM)

        scan.assert_called_once_with(b'LAGER_PROCESS_ID=')
        for proc in procs:
            self.assertExited(proc)

    def test_no_processes_is_reported_not_raised(self):
        with mock.patch.object(executor, '_scan_lager_pids', return_value=[]):
            with self.assertLogs(executor.logger, level='WARNING'):
                executor._kill_all_lager_processes(signal.SIGTERM)


class PidIsAliveTests(KillTestCase):

    def test_true_for_a_running_process(self):
        proc, = self.spawn(_DIES_ON_SIGTERM, count=1)
        self.assertTrue(executor._pid_is_alive(proc.pid))

    def test_false_once_the_process_is_reaped(self):
        proc, = self.spawn(_DIES_ON_SIGTERM, count=1)
        proc.kill()
        proc.wait()
        self.assertFalse(executor._pid_is_alive(proc.pid))

    @unittest.skipUnless(os.path.isdir('/proc'), 'needs procfs')
    def test_a_zombie_counts_as_dead(self):
        """An unreaped child has already run its exit path.

        Waiting out the grace for one would spend the whole cleanup budget on
        a process that is only waiting for its parent to call wait().
        """
        proc, = self.spawn(_DIES_ON_SIGTERM, count=1)
        proc.kill()
        # Deliberately not reaped: Popen.poll() is what would clear it.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with open(f'/proc/{proc.pid}/stat', 'rb') as f:
                if f.read().rpartition(b')')[2].split()[0] == b'Z':
                    break
            time.sleep(0.05)
        else:
            self.skipTest('child never entered the zombie state')

        self.assertFalse(executor._pid_is_alive(proc.pid))


if __name__ == '__main__':
    unittest.main()
