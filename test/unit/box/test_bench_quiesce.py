# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The bench must not be handed to a new job while the last one is cleaning up.

A job's cleanup runs after its client is already gone, so nothing the client
owns — least of all the box lock, which it releases on the way out — can tell
the box whether the hardware is free. These tests pin the box-side answer.
"""

import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box'))

from lager.exec import quiesce  # noqa: E402


class QuiesceTestCase(unittest.TestCase):
    def setUp(self):
        quiesce.finish(list(quiesce._reaping))
        self.addCleanup(lambda: quiesce.finish(list(quiesce._reaping)))
        self.children = []
        self.addCleanup(self.reap_children)

    def reap_children(self):
        for proc in self.children:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def spawn_sleeper(self, seconds=30):
        proc = subprocess.Popen(
            [sys.executable, '-c', f'import time; time.sleep({seconds})'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.children.append(proc)
        return proc


class BoundsTests(unittest.TestCase):
    """The three timeouts have to agree, and nothing else makes them.

    They live in two modules that cannot import each other (process.py imports
    quiesce, not the reverse), so the relationship is unenforceable at runtime
    and only visible here. It has already been got wrong once: the progress
    watchdog took the worst-case reap from 7s to 64s and left QUIESCE_WAIT_S at
    a flat 15.0, which silently reopened the overlap the gate exists to prevent
    for any cleanup running longer than 15s.
    """

    def longest_reap(self):
        """Ceiling on cleanup, then SIGTERM's wait, then SIGKILL's."""
        from lager.exec import process
        return process.CLEANUP_MAX_S + 2 * process.TERMINATE_GRACE_S

    def test_a_starting_job_waits_out_the_longest_possible_reap(self):
        from lager.python import executor
        self.assertGreaterEqual(
            executor.QUIESCE_WAIT_S, self.longest_reap(),
            'a new job stops waiting before the previous one can finish being '
            'killed, so it starts driving a bench that is still mid-teardown',
        )

    def test_quiesce_bounds_cover_the_reap(self):
        self.assertGreater(
            quiesce.STUCK_AFTER_S, self.longest_reap(),
            'the registry drops a job while it is still being legitimately '
            'killed, so the bench is handed over mid-escalation',
        )

    def test_the_registry_outlasts_anyone_waiting_on_it(self):
        from lager.python import executor
        self.assertGreater(
            quiesce.STUCK_AFTER_S, executor.QUIESCE_WAIT_S,
            'the registry can go empty while a job is still waiting on it, '
            'which reports a quiesced bench that never quiesced',
        )


class RegistryTests(QuiesceTestCase):
    def test_a_live_registered_job_is_reported_as_shutting_down(self):
        proc = self.spawn_sleeper()
        quiesce.begin([proc.pid])
        self.assertEqual(quiesce.reaping(), [proc.pid])

    def test_finish_removes_the_job(self):
        proc = self.spawn_sleeper()
        quiesce.begin([proc.pid])
        quiesce.finish([proc.pid])
        self.assertEqual(quiesce.reaping(), [])

    def test_an_exited_job_stops_counting_without_anyone_calling_finish(self):
        """The reaper can die mid-escalation; nothing would call finish().

        Membership has to be intersected with liveness or a crashed reaper
        would wedge the box permanently.
        """
        proc = self.spawn_sleeper()
        quiesce.begin([proc.pid])
        proc.kill()
        proc.wait(timeout=5)

        self.assertEqual(quiesce.reaping(), [])
        self.assertNotIn(proc.pid, quiesce._reaping)

    def test_a_job_wedged_past_the_ceiling_stops_holding_the_bench(self):
        """A PID that survives SIGKILL is stuck, not cleaning up.

        The bench genuinely isn't safe, but blocking forever turns one wedged
        script into a dead box, so we give up the claim and warn.
        """
        proc = self.spawn_sleeper()
        quiesce.begin([proc.pid])
        # Backdate the registration past the ceiling.
        quiesce._reaping[proc.pid] = time.monotonic() - quiesce.STUCK_AFTER_S - 1

        with self.assertLogs('lager.exec.quiesce', level='WARNING') as logs:
            self.assertEqual(quiesce.reaping(), [])
        self.assertIn('wedged', '\n'.join(logs.output))


class WaitUntilClearTests(QuiesceTestCase):
    def test_returns_immediately_when_nothing_is_shutting_down(self):
        started = time.monotonic()
        quiesced, pending = quiesce.wait_until_clear(timeout=5)
        self.assertTrue(quiesced)
        self.assertEqual(pending, [])
        self.assertLess(time.monotonic() - started, 0.5)

    def test_blocks_until_the_previous_job_is_gone(self):
        proc = self.spawn_sleeper()
        quiesce.begin([proc.pid])

        def release_soon():
            time.sleep(0.4)
            proc.kill()
            # Reap it. On the box /proc reports the zombie state and liveness
            # is answered without this, but the macOS fallback probes with
            # os.kill(pid, 0), which an unreaped zombie still answers.
            proc.wait(timeout=5)

        threading.Thread(target=release_soon, daemon=True).start()

        started = time.monotonic()
        quiesced, pending = quiesce.wait_until_clear(timeout=10)
        elapsed = time.monotonic() - started

        self.assertTrue(quiesced)
        self.assertEqual(pending, [])
        self.assertGreater(elapsed, 0.3, 'returned before the job had exited')
        self.assertLess(elapsed, 5)

    def test_gives_up_after_the_timeout_and_names_the_offender(self):
        proc = self.spawn_sleeper()
        quiesce.begin([proc.pid])

        quiesced, pending = quiesce.wait_until_clear(timeout=0.3)

        self.assertFalse(quiesced)
        self.assertEqual(pending, [proc.pid])


class TerminateProcessRegistersTests(QuiesceTestCase):
    """terminate_process is the disconnect path: the only cleanup that runs
    when the client is hard-killed. It must publish the window it opens."""

    def test_the_bench_is_held_for_as_long_as_cleanup_runs(self):
        from lager.exec.process import terminate_process

        # A child that takes a visible amount of time to unwind on SIGINT,
        # standing in for a real teardown.
        proc = subprocess.Popen(
            [
                sys.executable, '-c',
                'import time\n'
                'try:\n'
                '    time.sleep(30)\n'
                'except KeyboardInterrupt:\n'
                '    time.sleep(0.6)\n'
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.children.append(proc)
        time.sleep(0.3)  # let the interpreter reach the sleep

        seen = []

        def watch():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                seen.append(tuple(quiesce.reaping()))
                time.sleep(0.05)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        terminate_process(proc, cleanup_grace_s=3.0)
        held = [s for s in seen if proc.pid in s]

        self.assertTrue(
            held,
            'terminate_process never reported the job as shutting down, so a '
            'new job could have started during its cleanup',
        )
        self.assertEqual(
            quiesce.reaping(), [],
            'the bench was still held after the child exited',
        )


class ExecuteWaitsTests(QuiesceTestCase):
    """The gate itself: a starting job waits for the previous one."""

    def test_execute_waits_for_a_shutting_down_job_before_spawning(self):
        from lager.python import executor

        # Liveness is driven explicitly rather than by a real child: this test
        # is about the ordering of the gate against the spawn, and reading it
        # off /proc makes it depend on process-reaping timing that differs
        # between platforms. pid_is_alive has its own tests above.
        previous_job = 424242
        alive = {previous_job: True}

        order = []

        def fake_popen(*args, **kwargs):
            order.append('spawned')
            raise RuntimeError('stop here; we only care about the ordering')

        def release_soon():
            time.sleep(0.4)
            order.append('previous job finished cleaning up')
            alive[previous_job] = False

        ex = executor.PythonExecutor()
        # Liveness has to be patched before the job is registered: reaping()
        # prunes anything it sees as already exited, and a synthetic PID is
        # exactly that to the real implementation.
        with mock.patch.object(quiesce, 'pid_is_alive', lambda pid: alive.get(pid, False)), \
                mock.patch.object(executor.subprocess, 'Popen', fake_popen), \
                mock.patch.object(executor, '_release_hardware_service_direct_usb_claims',
                                  lambda: None):
            quiesce.begin([previous_job])
            threading.Thread(target=release_soon, daemon=True).start()
            with self.assertRaises(RuntimeError):
                ex.execute(script_file=_script(b'pass'), timeout=5)

        self.assertEqual(
            order, ['previous job finished cleaning up', 'spawned'],
            'the new job spawned before the previous one had finished cleaning up',
        )

    def test_execute_gives_up_on_a_wedged_job_rather_than_blocking_forever(self):
        """A job that survives SIGKILL must not make the box permanently unusable."""
        from lager.python import executor

        spawned = []

        def fake_popen(*args, **kwargs):
            spawned.append(True)
            raise RuntimeError('stop here')

        ex = executor.PythonExecutor()
        with mock.patch.object(quiesce, 'pid_is_alive', lambda pid: True), \
                mock.patch.object(executor, 'QUIESCE_WAIT_S', 0.3), \
                mock.patch.object(executor.subprocess, 'Popen', fake_popen), \
                mock.patch.object(executor, '_release_hardware_service_direct_usb_claims',
                                  lambda: None):
            quiesce.begin([424243])
            with self.assertLogs('lager.python.executor', level='WARNING') as logs:
                with self.assertRaises(RuntimeError):
                    ex.execute(script_file=_script(b'pass'), timeout=5)

        self.assertTrue(spawned, 'the box refused to ever start another job')
        self.assertIn('still shutting down', '\n'.join(logs.output))


def _script(body):
    import io
    return io.BytesIO(body)


if __name__ == '__main__':
    unittest.main()
