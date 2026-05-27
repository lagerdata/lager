# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for lager.util.device_lock — the cross-process advisory lock that
prevents two box-side pyvisa clients from racing on `open_resource()` for the
same USB-TMC instrument (the JUL-7 / Keithley 2281S EBUSY scenario).

These tests use real fcntl flocks against real lock files under a temp
directory — the lock semantics are fundamentally OS-level and can't be
meaningfully unit-tested with mocks.
"""

import json
import os
import sys
import time
import types
import tempfile
import multiprocessing
import unittest
from unittest.mock import MagicMock


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted):
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


# Importing `lager.util.device_lock` triggers `lager/__init__.py` which pulls in
# pyvisa via the nets-mappers chain. The util module itself doesn't need any of
# this — stub the optional hardware deps so the test can run without pyvisa /
# usb / labjack etc. installed locally.
_HARDWARE_STUBS = [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'pigpio',
    'labjack', 'labjack.ljm',
    'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak',
    'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev',
    'smbus', 'smbus2',
    'RPi', 'RPi.GPIO',
    'gpiod',
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

sys.modules.setdefault('simplejson', json)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.util.device_lock import DeviceLockManager, DeviceLockError, device_lock  # noqa: E402


def _hold_lock_for_seconds(lock_subdir, address, seconds, ready_path):
    """Worker target: acquire the lock, signal ready by touching `ready_path`,
    sleep `seconds`, release. Run in a separate process so the flock holds
    across the OS boundary."""
    mgr = DeviceLockManager(lock_subdir=lock_subdir)
    mgr.acquire_lock(address, timeout=1.0)
    # Signal the parent that we've acquired so it can start its contender.
    with open(ready_path, 'w') as f:
        f.write(str(os.getpid()))
    time.sleep(seconds)
    mgr.release_lock(address)


class DeviceLockManagerTests(unittest.TestCase):
    """Single-process invariants — same-instance behavior."""

    def setUp(self):
        # Unique subdir per test to avoid leaking state across tests.
        self.subdir = f'lager_test_locks_{os.getpid()}_{id(self)}'
        self.mgr = DeviceLockManager(lock_subdir=self.subdir)
        self.address = 'USB0::0xDEAD::0xBEEF::TEST::INSTR'

    def tearDown(self):
        # Release anything still held; cleanup lock dir.
        try:
            self.mgr.release_lock(self.address)
        except Exception:
            pass
        lock_dir = os.path.join(tempfile.gettempdir(), self.subdir)
        if os.path.isdir(lock_dir):
            for f in os.listdir(lock_dir):
                try:
                    os.unlink(os.path.join(lock_dir, f))
                except OSError:
                    pass
            try:
                os.rmdir(lock_dir)
            except OSError:
                pass

    def test_acquire_release_basic(self):
        self.assertTrue(self.mgr.acquire_lock(self.address))
        # Second acquire from same instance is a no-op success.
        self.assertTrue(self.mgr.acquire_lock(self.address))
        self.mgr.release_lock(self.address)
        # Releasing again is safe.
        self.mgr.release_lock(self.address)

    def test_lock_file_written_under_configured_subdir(self):
        self.mgr.acquire_lock(self.address)
        lock_dir = os.path.join(tempfile.gettempdir(), self.subdir)
        contents = os.listdir(lock_dir)
        # One file, name encodes the (sanitized) address.
        self.assertTrue(any('device_' in f and 'INSTR' in f for f in contents),
                        f'expected device_*_INSTR.lock in {lock_dir}, got {contents}')

    def test_context_manager_releases_on_exit(self):
        with device_lock(self.address, timeout=1.0, manager=self.mgr):
            self.assertIn(self.address, self.mgr.lock_handles)
        self.assertNotIn(self.address, self.mgr.lock_handles)

    def test_context_manager_releases_on_exception(self):
        class _Boom(Exception):
            pass

        with self.assertRaises(_Boom):
            with device_lock(self.address, timeout=1.0, manager=self.mgr):
                raise _Boom()
        # Lock must be released even when the body raised.
        self.assertNotIn(self.address, self.mgr.lock_handles)


class CrossProcessContentionTests(unittest.TestCase):
    """The real story — two processes racing for the same address. Without
    `fcntl.flock` semantics this test cannot pass; with them, the loser of
    the race must time out and raise DeviceLockError."""

    def setUp(self):
        self.subdir = f'lager_test_xproc_{os.getpid()}_{id(self)}'
        self.address = 'USB0::0xDEAD::0xBEEF::XPROC::INSTR'
        # Temp path the holder process touches to signal it has the lock.
        fd, self.ready_path = tempfile.mkstemp(prefix='lager_lock_ready_')
        os.close(fd)
        os.unlink(self.ready_path)  # let the worker create it on signal

    def tearDown(self):
        if os.path.exists(self.ready_path):
            os.unlink(self.ready_path)
        lock_dir = os.path.join(tempfile.gettempdir(), self.subdir)
        if os.path.isdir(lock_dir):
            for f in os.listdir(lock_dir):
                try:
                    os.unlink(os.path.join(lock_dir, f))
                except OSError:
                    pass
            try:
                os.rmdir(lock_dir)
            except OSError:
                pass

    def test_second_acquirer_times_out_with_DeviceLockError(self):
        # Spawn a process that holds the lock for 3 seconds.
        holder = multiprocessing.Process(
            target=_hold_lock_for_seconds,
            args=(self.subdir, self.address, 3.0, self.ready_path),
        )
        holder.start()
        try:
            # Wait for the holder to signal ready (max 2s).
            deadline = time.time() + 2.0
            while not os.path.exists(self.ready_path) and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(self.ready_path),
                            'holder did not acquire within 2s — test setup broken')

            # Contender: same address from this process, short timeout.
            contender = DeviceLockManager(lock_subdir=self.subdir)
            start = time.time()
            with self.assertRaises(DeviceLockError):
                contender.acquire_lock(self.address, timeout=1.0)
            elapsed = time.time() - start
            # Must have respected the timeout (within reasonable jitter).
            self.assertLess(elapsed, 2.1,
                            f'acquire blocked for {elapsed:.2f}s, expected ~1s')
            self.assertGreaterEqual(elapsed, 0.9,
                                    f'acquire returned in {elapsed:.2f}s, '
                                    f'expected to wait near the 1s timeout')
        finally:
            holder.join(timeout=5.0)
            if holder.is_alive():
                holder.terminate()
                holder.join()

    def test_second_acquirer_succeeds_after_first_releases(self):
        # Holder runs for only 0.5s — contender's 2s timeout should win.
        holder = multiprocessing.Process(
            target=_hold_lock_for_seconds,
            args=(self.subdir, self.address, 0.5, self.ready_path),
        )
        holder.start()
        try:
            deadline = time.time() + 2.0
            while not os.path.exists(self.ready_path) and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(self.ready_path))

            contender = DeviceLockManager(lock_subdir=self.subdir)
            start = time.time()
            ok = contender.acquire_lock(self.address, timeout=2.0)
            elapsed = time.time() - start
            self.assertTrue(ok)
            # Should have acquired AFTER the holder released, well under timeout.
            self.assertLess(elapsed, 1.5,
                            f'contender took {elapsed:.2f}s; holder only held 0.5s')
            contender.release_lock(self.address)
        finally:
            holder.join(timeout=5.0)
            if holder.is_alive():
                holder.terminate()
                holder.join()


if __name__ == '__main__':
    unittest.main()
