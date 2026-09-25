# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""One J-Link operation per probe at a time (``lager.debug.probe_lock``).

J-Link lets several applications open one probe at once and does nothing to
stop them interleaving. A flash whose RAMCode download is interrupted by a
second client halting, resetting or reading the target reports ``Verification
of RAMCode failed`` / ``Failed to download RAMCode!`` and programs nothing.
The box's debug service is threaded and a ``lager python`` script is another
process, so both kinds of overlap happened.

Pinned: a second thread or process waits for the first; the wait gives up with
a message naming the holder; the lock is re-entrant for the flash sequence's
own helpers; a holder that dies releases it; separate probes never block
each other; and the decorator holds the lock for a generator's whole life
while keeping its return value.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..', 'box', 'lager', 'debug', 'probe_lock.py'))
_spec = importlib.util.spec_from_file_location('probe_lock_under_test', _PATH)
probe_lock_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_lock_mod)

probe_lock = probe_lock_mod.probe_lock
holds_probe = probe_lock_mod.holds_probe
ProbeBusyError = probe_lock_mod.ProbeBusyError

# A second process that takes the flock the way the module does, prints READY,
# and holds it until killed.
_HOLDER = r'''
import fcntl, os, sys, time
path = sys.argv[1]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, b'flash (pid %d)\n' % os.getpid())
print('READY', flush=True)
time.sleep(60)
'''


class _Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patches = [
            patch.object(probe_lock_mod, 'LOCK_DIR', self.dir),
            patch.object(probe_lock_mod, '_registry', {}),
            patch.dict(os.environ, {'LAGER_PROBE_LOCK_TIMEOUT_S': '5'}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def hold_in_another_process(self, serial):
        path = os.path.join(self.dir, f'{serial}.lock')
        proc = subprocess.Popen([sys.executable, '-c', _HOLDER, path],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), 'READY')
        return proc


class ProbeLockTests(_Case):
    def test_the_same_thread_can_take_it_again(self):
        with probe_lock('111', 'flash'):
            with probe_lock('111', 'gdbserver start'):
                pass

    def test_a_second_thread_waits_for_the_first(self):
        order = []
        entered = threading.Event()

        def first():
            with probe_lock('111', 'flash'):
                entered.set()
                time.sleep(0.3)
                order.append('flash done')

        def second():
            entered.wait()
            with probe_lock('111', 'connect'):
                order.append('connect')

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(order, ['flash done', 'connect'])

    def test_another_process_is_waited_for(self):
        proc = self.hold_in_another_process('111')
        threading.Timer(0.3, proc.kill).start()
        started = time.monotonic()
        with probe_lock('111', 'flash'):
            waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.2)

    def test_the_wait_gives_up_and_names_the_holder(self):
        self.hold_in_another_process('111')
        with patch.dict(os.environ, {'LAGER_PROBE_LOCK_TIMEOUT_S': '0.3'}):
            with self.assertRaises(ProbeBusyError) as caught:
                with probe_lock('111', 'connect'):
                    pass
        message = str(caught.exception)
        self.assertIn('J-Link probe 111 is busy with flash (pid', message)
        self.assertIn('connect gave up after 0.3 s', message)

    def test_a_thread_that_waits_too_long_gives_up(self):
        entered, done = threading.Event(), threading.Event()

        def holder():
            with probe_lock('111', 'flash'):
                entered.set()
                done.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(done.set)
        entered.wait()
        with patch.dict(os.environ, {'LAGER_PROBE_LOCK_TIMEOUT_S': '0.2'}):
            with self.assertRaises(ProbeBusyError) as caught:
                with probe_lock('111', 'memory read'):
                    pass
        self.assertIn('busy with flash', str(caught.exception))

    def test_a_thread_that_waits_says_so_in_the_log(self):
        """So the service log shows requests being ordered, not only
        processes."""
        entered, done = threading.Event(), threading.Event()

        def holder():
            with probe_lock('111', 'flash'):
                entered.set()
                done.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        entered.wait()
        threading.Timer(0.2, done.set).start()
        with self.assertLogs(probe_lock_mod.logger, level='INFO') as logs:
            with probe_lock('111', 'connect'):
                pass
        t.join()
        self.assertTrue(any('Probe 111 busy (flash (pid' in m and 'connect waiting' in m
                            for m in logs.output), logs.output)

    def test_a_dead_holder_releases_the_probe(self):
        proc = self.hold_in_another_process('111')
        proc.kill()
        proc.wait()
        with probe_lock('111', 'flash'):
            pass

    def test_separate_probes_do_not_block_each_other(self):
        self.hold_in_another_process('111')
        with probe_lock('222', 'flash'):
            pass

    def test_it_is_free_again_after_an_exception(self):
        with self.assertRaises(RuntimeError):
            with probe_lock('111', 'flash'):
                raise RuntimeError('boom')
        self.assertEqual(probe_lock_mod._registry['111'].depth, 0)
        with probe_lock('111', 'flash'):
            pass

    def test_serials_become_safe_file_names(self):
        self.assertEqual(probe_lock_mod._key(None), 'default')
        self.assertEqual(probe_lock_mod._key('../x y'), '___x_y')

    def test_a_bad_timeout_setting_falls_back_to_the_default(self):
        for raw in ('', 'soon', '-1'):
            with self.subTest(raw=raw), patch.dict(
                    os.environ, {'LAGER_PROBE_LOCK_TIMEOUT_S': raw}):
                self.assertEqual(probe_lock_mod._timeout_s(),
                                 probe_lock_mod.DEFAULT_TIMEOUT_S)


class HoldsProbeTests(_Case):
    def test_a_function_runs_under_its_serials_lock(self):
        seen = []

        @holds_probe('reset')
        def reset(halt=False, serial=None):
            seen.append(probe_lock_mod._registry[serial].depth)
            return 'done'

        self.assertEqual(reset(serial='111'), 'done')
        self.assertEqual(seen, [1])

    def test_a_generator_holds_it_while_iterated_and_keeps_its_return(self):
        depths = []

        @holds_probe('flash')
        def flash(files, serial=None):
            depths.append(probe_lock_mod._registry[serial].depth)
            yield 'one'
            depths.append(probe_lock_mod._registry[serial].depth)
            return 'the failure line'

        gen = flash([], serial='111')
        self.assertEqual(next(gen), 'one')
        with self.assertRaises(StopIteration) as done:
            next(gen)
        self.assertEqual(done.exception.value, 'the failure line')
        self.assertEqual(depths, [1, 1])
        self.assertEqual(probe_lock_mod._registry['111'].depth, 0)

    def test_closing_a_generator_early_releases_it(self):
        @holds_probe('flash')
        def flash(serial=None):
            yield 'one'
            yield 'two'

        gen = flash(serial='111')
        next(gen)
        gen.close()
        self.assertEqual(probe_lock_mod._registry['111'].depth, 0)

    def test_positional_serial_is_found(self):
        @holds_probe('memory read')
        def read_memory(address, length, mcu=None, serial=None):
            return probe_lock_mod._registry[serial].depth

        self.assertEqual(read_memory(0, 4, None, '111'), 1)


if __name__ == '__main__':
    unittest.main()
