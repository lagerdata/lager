# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Noticing that the client went away while the script is silent.

The box reaps a script whose client disconnected, and since that reap now runs
the script's cleanup (see terminate_process), it is the one teardown path that
works no matter how the executor died -- Ctrl+C, a cancelled GitHub Actions job
whose runner SIGKILLs the process tree, a Rust test with no signal handling at
all. What it could not do was notice promptly.

Detection used to depend entirely on a write failing, and two things conspired
against that. Nothing is written while a script produces no output, and our
longer HIL tests are quiet for minutes. And even when something is written, TCP
hides the first failure: the bytes go into the send buffer, the peer answers
RST, and only the *next* write raises. So a disconnect could go unnoticed for
two 20s keepalive intervals while the script kept driving the bench.

These tests use real sockets and real child processes, because both defects
live in exactly the parts a mock would replace.
"""

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.exec.process import IDLE_TICK, stream_process_output  # noqa: E402
from lager.python.service import PythonServiceHandler, peer_is_connected  # noqa: E402


def _silent_child():
    """A child that prints once and then goes quiet, like a mid-test HIL run.

    The one line is what a real run has already emitted by the time anyone
    cancels; the silence afterwards is the condition under test.
    """
    return subprocess.Popen(
        [sys.executable, '-u', '-c',
         'import sys, time\n'
         'sys.stdout.write("x" * 1024)\n'
         'sys.stdout.flush()\n'
         'while True: time.sleep(0.05)\n'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class PeerIsConnectedTests(unittest.TestCase):

    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.addCleanup(self._close_both)

    def _close_both(self):
        for sock in (self.a, self.b):
            try:
                sock.close()
            except OSError:
                pass

    def test_true_while_the_peer_is_there(self):
        self.assertTrue(peer_is_connected(self.a))

    def test_false_once_the_peer_closes(self):
        """REGRESSION: this is what a killed CLI looks like, and it used to be
        invisible until the next write -- or the write after that."""
        self.b.close()

        self.assertFalse(peer_is_connected(self.a))

    def test_true_when_the_peer_sent_data(self):
        """Readable must not be mistaken for closed.

        A false positive here would abort a healthy run and force-kill the
        script mid-test, which is worse than a slow disconnect notice.
        """
        self.b.sendall(b'noise')

        self.assertTrue(peer_is_connected(self.a))

    def test_false_on_an_already_closed_socket(self):
        self.a.close()

        self.assertFalse(peer_is_connected(self.a))

    def test_survives_repeated_checks(self):
        """It runs ~10x/second for the life of every run, so it must not leak
        pollers or consume the peeked byte."""
        self.b.sendall(b'noise')

        for _ in range(200):
            self.assertTrue(peer_is_connected(self.a))

        self.assertEqual(self.a.recv(5), b'noise')


class IdleTickTests(unittest.TestCase):
    """The generator has to hand control back while the script is quiet.

    Without a tick the streaming loop is parked inside the generator until the
    script prints or the 20s keepalive fires, so there is nowhere for the peer
    check to run.
    """

    def setUp(self):
        self.procs = []
        self.addCleanup(self._reap_all)

    def _reap_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    def _spawn(self):
        proc = _silent_child()
        self.procs.append(proc)
        return proc

    def test_ticks_while_the_script_is_silent(self):
        proc = self._spawn()
        gen = stream_process_output(proc, None, set())

        chunks = [next(gen) for _ in range(12)]
        gen.close()

        self.assertIn(IDLE_TICK, chunks, 'generator never yielded an idle tick')

    def test_ticks_arrive_promptly(self):
        """Detection latency is bounded by how often these arrive, so pin it.

        The bound that matters is 'sub-second', not the exact poll interval.
        """
        proc = self._spawn()
        gen = stream_process_output(proc, None, set())

        next(gen)  # the child's one real chunk
        start = time.monotonic()
        while next(gen) != IDLE_TICK:
            pass
        elapsed = time.monotonic() - start
        gen.close()

        self.assertLess(elapsed, 1.0)

    def test_the_tick_puts_nothing_on_the_wire(self):
        """It must be skippable by length alone; the consumer writes what it
        gets, and a tick that carried bytes would corrupt the stream."""
        self.assertEqual(len(IDLE_TICK), 0)

    def test_a_silent_script_is_still_reaped_when_the_generator_closes(self):
        """The ticks must not have displaced the teardown they exist to enable."""
        proc = self._spawn()
        gen = stream_process_output(proc, None, set())

        next(gen)
        self.assertIsNone(proc.poll())
        gen.close()

        deadline = time.time() + 10
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.05)
        self.assertIsNotNone(proc.poll(), 'silent child survived the disconnect')


class _Streamer:
    """Stand-in for the request handler: only what send_streaming_response uses.

    Deliberately not a mock of the socket. The whole defect being fixed lives
    in socket behaviour, so the socket has to be real.
    """

    def __init__(self, sock):
        self.connection = sock
        self.wfile = sock.makefile('wb', 0)

    def send_response(self, code):
        pass

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass


class DisconnectTeardownTests(unittest.TestCase):
    """The whole chain, end to end: silent script, client vanishes, cleanup runs.

    This is the path that covers the cases the client cannot help with -- a
    cancelled GHA job, a Rust test binary, anything killed outright -- so it is
    worth pinning as one piece rather than only in parts.
    """

    def test_a_silent_script_is_cleaned_up_when_the_client_vanishes(self):
        marker = os.path.join(tempfile.mkdtemp(), 'cleaned')
        proc = subprocess.Popen(
            [sys.executable, '-u', '-c',
             'import sys, time\n'
             'try:\n'
             '    sys.stdout.write("x" * 1024)\n'
             '    sys.stdout.flush()\n'
             '    while True: time.sleep(0.05)\n'
             'except KeyboardInterrupt:\n'
             '    open(sys.argv[1], "w").write("cleaned")\n',
             marker],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._reap, proc)
        server, client = socket.socketpair()
        self.addCleanup(server.close)

        gen = stream_process_output(proc, None, set())
        finished = threading.Event()

        def stream():
            try:
                PythonServiceHandler.send_streaming_response(_Streamer(server), gen)
            finally:
                finished.set()

        threading.Thread(target=stream, daemon=True).start()

        # Let the script's one line reach the client, then pull the plug. From
        # here on nothing is written, which is exactly the case that used to
        # leave the script running.
        time.sleep(0.5)
        self.assertIsNone(proc.poll(), 'child died before the disconnect')
        disconnected_at = time.monotonic()
        client.close()

        self.assertTrue(
            finished.wait(15),
            'streaming never noticed the client was gone',
        )
        noticed_in = time.monotonic() - disconnected_at

        self.assertIsNotNone(proc.poll(), 'silent script outlived its client')
        self.assertTrue(
            os.path.exists(marker),
            'script was killed without getting to run its cleanup',
        )
        # Comfortably inside one keepalive interval, which is what the old
        # write-driven detection needed two of.
        self.assertLess(noticed_in, 10.0)

    def _reap(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
