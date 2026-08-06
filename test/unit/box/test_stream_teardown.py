# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Teardown of `stream_process_output` when the client goes away.

These tests spawn REAL child processes rather than mocking Popen. The defect
they cover is entirely about process lifetime -- whether a signal is actually
delivered to a real PID when a generator is closed early -- and a mocked Popen
would assert only that we called a method we wrote, not that the child died.

The scenario: a `lager python` run streams output; the client disconnects
(Ctrl-C that never reached /python/kill, a killed CLI, a dropped network, a
cancelled CI job). The HTTP layer stops iterating and closes the generator,
which raises GeneratorExit at the `yield` inside the read loop. That skipped the
`terminate_process(proc)` sitting after the loop, and GeneratorExit derives from
BaseException so the `except Exception` never caught it either. The child
survived as an orphan at 100% CPU, holding its device flock, blocking every
later run on that instrument until someone killed it by hand.
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.exec import process as process_mod  # noqa: E402
from lager.exec.process import stream_process_output, terminate_process  # noqa: E402


def _spin_forever():
    """A child that emits one line, then spins and never exits on its own.

    The spin is the shape from the field report: the orphan sat at 100% CPU
    doing no I/O, so nothing could reap it via a closed pipe or SIGPIPE.

    The output before the spin is purely so the generator has something real to
    yield, and it is a full 1024 bytes on purpose: the drain thread calls
    `readable.read(1024)` on a BufferedReader, which blocks until it has that
    many bytes or hits EOF, so a short write would just sit in the buffer.

    That used to also be what kept these tests fast -- without it the first
    `next()` blocked for KEEPALIVE_TIME (20 s). It no longer is: the generator
    now yields a zero-length IDLE_TICK on every idle poll, so `next()` returns
    promptly either way. Kept because the tests still want the child to have
    actually run and produced output before the teardown under test.
    """
    return subprocess.Popen(
        [sys.executable, '-c',
         'import sys; sys.stdout.write("g" * 1024); sys.stdout.flush()\n'
         'while True: pass\n'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_exit(proc, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


class StreamTeardownTests(unittest.TestCase):

    def setUp(self):
        self.procs = []

    def tearDown(self):
        # Never leave a spinning child behind, even if an assertion failed.
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def _spawn(self):
        proc = _spin_forever()
        self.procs.append(proc)
        return proc

    def test_closing_the_generator_early_reaps_the_child(self):
        """REGRESSION: this is the orphaned 100%-CPU process.

        Closing the generator must terminate the child. Before the fix the
        child was never signalled and outlived the request forever.
        """
        proc = self._spawn()
        gen = stream_process_output(proc, None, set())

        # Start the generator so the drain threads are running and it is
        # parked at a yield -- the state a live stream is in.
        next(gen, None)
        self.assertIsNone(proc.poll(), "child should still be running")

        # The client goes away.
        gen.close()

        self.assertTrue(
            _wait_for_exit(proc),
            "child survived the generator being closed -- it is an orphan",
        )

    def test_abandoning_the_generator_reaps_the_child(self):
        """The same path reached the way CPython actually reaches it.

        `send_streaming_response` breaks out of its `for` loop on a broken
        pipe; dropping the last reference is what closes the generator.
        """
        proc = self._spawn()
        gen = stream_process_output(proc, None, set())
        next(gen, None)
        self.assertIsNone(proc.poll())

        del gen

        self.assertTrue(
            _wait_for_exit(proc),
            "child survived the generator being dropped",
        )

    def test_cleanup_functions_still_run_when_closed_early(self):
        """Adding the terminate must not displace the existing cleanup."""
        proc = self._spawn()
        ran = []
        gen = stream_process_output(proc, None, {lambda: ran.append('cleaned')})

        next(gen, None)
        gen.close()

        self.assertEqual(ran, ['cleaned'])

    def test_normal_completion_does_not_double_terminate(self):
        """A child that exits on its own keeps its real return code.

        The `proc.poll() is None` guard is what keeps the new teardown from
        re-reaping an already-finished child and overwriting its exit status.
        """
        proc = subprocess.Popen(
            [sys.executable, '-c', 'import sys; sys.stdout.write("hi"); sys.exit(7)'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.procs.append(proc)

        chunks = b''.join(stream_process_output(proc, None, set()))

        self.assertIn(b'hi', chunks)
        # Trailing frame is "- <len> <returncode>".
        self.assertTrue(chunks.rstrip().endswith(b'- 1 7'),
                        f"expected exit code 7 in the final frame, got {chunks[-24:]!r}")


class TerminateProcessTests(unittest.TestCase):
    """terminate_process is the reaper behind the teardown above.

    It used to lead with SIGTERM, whose default Python disposition kills the
    interpreter outright: `finally` blocks, context-manager `__exit__`s and
    `atexit` hooks were all skipped. So a client that went away left the box
    free of orphans but left the BENCH as the test had it -- outputs still
    driven, rails still energised, peripherals still running.

    That matters more than the other stop paths because this one needs nothing
    from the client. It is the only cleanup that still happens when the
    supervisor is hard-killed rather than signalled, which is exactly what
    GitHub Actions does 10s into a cancellation.
    """

    def setUp(self):
        self.procs = []
        # Keep the escalation rungs short; the real values are sized for a HIL
        # script's cleanup, not for a test suite.
        for name, value in (('CLEANUP_GRACE_S', 0.5), ('TERMINATE_GRACE_S', 0.5)):
            patcher = mock.patch.object(process_mod, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._reap_all)

    def _reap_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def _spawn(self, source, *args):
        proc = subprocess.Popen(
            [sys.executable, '-c', source, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        assert proc.stdout is not None
        proc.stdout.readline()  # handlers are installed once "ready" is out
        return proc

    def test_the_child_gets_to_run_its_cleanup(self):
        """REGRESSION: this is the bench left live.

        The marker file stands in for a script's real teardown -- de-energising
        rails, parking outputs, releasing the DUT -- work that only happens if
        the child is unwound rather than killed outright.
        """
        marker = os.path.join(tempfile.mkdtemp(), 'cleaned')
        proc = self._spawn(
            'import sys, time\n'
            'try:\n'
            '    print("ready", flush=True)\n'
            '    while True: time.sleep(0.05)\n'
            'except KeyboardInterrupt:\n'
            '    open(sys.argv[1], "w").write("cleaned")\n',
            marker,
        )

        returncode = terminate_process(proc)

        self.assertTrue(
            os.path.exists(marker),
            'child was killed before its cleanup could run',
        )
        self.assertEqual(returncode, 0, 'child should have exited on its own')

    def test_escalates_to_sigterm_when_the_interrupt_is_ignored(self):
        proc = self._spawn(
            'import signal, time\n'
            'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
            'print("ready", flush=True)\n'
            'time.sleep(120)\n'
        )

        terminate_process(proc)

        self.assertEqual(proc.poll(), -signal.SIGTERM)

    def test_escalates_to_sigkill_when_both_are_ignored(self):
        proc = self._spawn(
            'import signal, time\n'
            'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            'print("ready", flush=True)\n'
            'time.sleep(120)\n'
        )

        terminate_process(proc)

        self.assertEqual(proc.poll(), -signal.SIGKILL)

    def test_an_already_exited_child_costs_nothing(self):
        """The normal path calls this too, on a child that has already gone.

        Every rung must be a no-op there, or each completed run would pay the
        cleanup grace before its exit code reached the client.
        """
        proc = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(7)'])
        self.procs.append(proc)
        proc.wait(timeout=5)

        start = time.monotonic()
        returncode = terminate_process(proc)
        elapsed = time.monotonic() - start

        self.assertEqual(returncode, 7)
        self.assertLess(elapsed, 0.5)


if __name__ == '__main__':
    unittest.main()
