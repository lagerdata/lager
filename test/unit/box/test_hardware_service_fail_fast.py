# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for fail-fast locking and hang recovery in box/lager/hardware_service.py.

``/invoke`` serialises per physical device (``_get_device_lock`` /
``_get_address_lock``) so two requests cannot interleave SCPI on one
instrument. That lock was taken with a plain ``with``, which is fine while
every holder eventually returns — and is not what a wedged USB device does. A
``open_resource`` that blocks in libusb before the 5s VISA I/O timeout is even
set, or a native driver call that never returns, leaves the lock held for the
life of the process: every later ``/invoke`` for that device queues behind it
with no error, no timeout of its own and no recovery.

Callers bound their own POST (``nets/device.py`` ``Device.DEFAULT_TIMEOUT``),
so users saw errors rather than hangs — but the wedge never cleared, and the
self-restart that exists for exactly this state only fired for operations that
*raised*. These tests pin both halves: a busy device answers 503 rather than
queueing, and an operation that never returns answers 504 and schedules the
supervisor respawn.

Hardware SDKs are stubbed, and a hang is stood up with a blocking
``threading.Event`` rather than a real wedged instrument — the one thing these
cannot cover is a driver actually hanging in native code.
"""

import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch


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


_HARDWARE_STUBS = [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'pigpio', 'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

import lager.hardware_service as hw  # noqa: E402

_ADDRESS = 'USB0::0x05E6::0x2281::WEDGE::INSTR'


class _WedgedDevice:
    """A cached driver whose method never returns, like a native call against
    an instrument whose USB link is wedged."""

    def __init__(self, release):
        self._release = release
        self.calls = 0

    def read_state(self):
        self.calls += 1
        # Bounded only so a failing test cannot leave a thread for the whole
        # session; the code under test must not wait for it.
        self._release.wait(30)
        return {'never': 'reached'}


class _HealthyDevice:
    def __init__(self):
        self.calls = 0

    def read_state(self):
        self.calls += 1
        return {'ok': True}


class HardwareServiceFailFastTests(unittest.TestCase):

    def setUp(self):
        hw.device_cache.clear()
        hw.module_cache.clear()
        with hw.device_locks_meta_lock:
            hw.device_locks.clear()
        with hw._visa_resources_meta_lock:
            hw._visa_resources.clear()
        self.client = hw.app.test_client()

        # maybe_self_restart ends in os._exit(70); never let a test reach it.
        self.restart = MagicMock()
        patcher = patch.object(hw, '_self_restart', self.restart)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Frees any wedged worker thread once the test is done with it.
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def _cache_device(self, device, address=_ADDRESS, name='fake_device'):
        hw.device_cache[(name, address)] = device
        return {'device': name, 'function': 'read_state', 'args': [],
                'kwargs': {}, 'net_info': {'address': address, 'channel': 1}}

    # -- busy ------------------------------------------------------------- #

    def test_busy_device_answers_503_without_queueing(self):
        device = _HealthyDevice()
        payload = self._cache_device(device)

        hw._get_address_lock(_ADDRESS).acquire()
        try:
            with patch.object(hw, '_LOCK_TIMEOUT_S', 0.3):
                start = time.monotonic()
                resp = self.client.post('/invoke', json=payload)
                elapsed = time.monotonic() - start
        finally:
            hw._get_address_lock(_ADDRESS).release()

        self.assertEqual(resp.status_code, 503)
        self.assertTrue(resp.get_json()['error'].startswith('device-busy:'),
                        resp.get_json())
        self.assertLess(elapsed, 5.0, 'the request queued instead of failing fast')
        self.assertEqual(device.calls, 0, 'the driver was reached while locked')

    def test_lock_is_released_after_a_busy_answer(self):
        device = _HealthyDevice()
        payload = self._cache_device(device)

        hw._get_address_lock(_ADDRESS).acquire()
        with patch.object(hw, '_LOCK_TIMEOUT_S', 0.2):
            self.client.post('/invoke', json=payload)
        hw._get_address_lock(_ADDRESS).release()

        resp = self.client.post('/invoke', json=payload)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(device.calls, 1)

    # -- hang ------------------------------------------------------------- #

    def test_hang_answers_504_and_schedules_the_self_restart(self):
        payload = self._cache_device(_WedgedDevice(self.release))

        with patch.object(hw, '_INVOKE_DEADLINE_S', 0.3):
            start = time.monotonic()
            resp = self.client.post('/invoke', json=payload)
            elapsed = time.monotonic() - start

        self.assertEqual(resp.status_code, 504)
        self.assertTrue(resp.get_json()['error'].startswith('invoke-timeout:'),
                        resp.get_json())
        self.assertLess(elapsed, 5.0, 'the request waited for the wedged driver')

        self.restart.schedule_self_restart_for_hang.assert_called_once()
        args, kwargs = self.restart.schedule_self_restart_for_hang.call_args
        self.assertEqual(args[0], _ADDRESS)
        self.assertEqual(kwargs['service'], 'hardware_service')
        self.assertEqual(kwargs['stamp_path'], hw._HW_SELF_RESTART_STAMP)
        # The hang path, not the unreachable-error path.
        self.restart.maybe_self_restart.assert_not_called()

    def test_hang_keeps_the_device_lock_so_later_calls_answer_busy(self):
        """The abandoned thread still owns the session, the libusb claim, the
        LJM handle. Releasing would hand the next request a device that is
        already in use by an operation nobody can cancel."""
        payload = self._cache_device(_WedgedDevice(self.release))

        with patch.object(hw, '_INVOKE_DEADLINE_S', 0.3):
            first = self.client.post('/invoke', json=payload)
        self.assertEqual(first.status_code, 504)

        with patch.object(hw, '_LOCK_TIMEOUT_S', 0.3):
            start = time.monotonic()
            second = self.client.post('/invoke', json=payload)
            elapsed = time.monotonic() - start

        self.assertEqual(second.status_code, 503)
        self.assertTrue(second.get_json()['error'].startswith('device-busy:'))
        self.assertLess(elapsed, 5.0)

    def test_a_wedged_device_does_not_block_a_different_one(self):
        """Locks are per physical device; one wedged instrument must not take
        the bench with it."""
        other_address = 'USB0::0x1AB1::0x0E11::HEALTHY::INSTR'
        wedged_payload = self._cache_device(_WedgedDevice(self.release))
        healthy = _HealthyDevice()
        healthy_payload = self._cache_device(
            healthy, address=other_address, name='other_device')

        with patch.object(hw, '_INVOKE_DEADLINE_S', 0.3):
            self.assertEqual(
                self.client.post('/invoke', json=wedged_payload).status_code, 504)

        resp = self.client.post('/invoke', json=healthy_payload)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(healthy.calls, 1)

    def test_healthy_call_still_answers_200(self):
        """The success path is untouched — only new error shapes were added."""
        device = _HealthyDevice()
        resp = self.client.post('/invoke', json=self._cache_device(device))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json(), {'ok': True})


class LockedCallTests(unittest.TestCase):
    """Direct coverage of the helper both endpoints share."""

    def test_returns_the_value_and_releases_the_lock(self):
        lock = threading.Lock()
        self.assertEqual(
            hw._locked_call(lock, lambda: 'v', what='t'), 'v')
        self.assertTrue(lock.acquire(timeout=0.1))
        lock.release()

    def test_driver_error_propagates_and_releases_the_lock(self):
        """Only a hang keeps the lock: a driver that raises has finished with
        the device, and the stale-VISA-session retry above depends on being
        able to take the lock again immediately."""
        lock = threading.Lock()
        with self.assertRaises(ValueError):
            hw._locked_call(lock, lambda: (_ for _ in ()).throw(ValueError('x')),
                            what='t')
        self.assertTrue(lock.acquire(timeout=0.1))
        lock.release()

    def test_busy_raises_device_busy(self):
        lock = threading.Lock()
        lock.acquire()
        with self.assertRaises(hw.DeviceBusy):
            hw._locked_call(lock, lambda: 'v', what='t', lock_timeout=0.1)
        lock.release()

    def test_hang_raises_device_operation_timeout_and_keeps_the_lock(self):
        lock = threading.Lock()
        release = threading.Event()
        self.addCleanup(release.set)
        with self.assertRaises(hw.DeviceOperationTimeout):
            hw._locked_call(lock, lambda: release.wait(30), what='t',
                            op_timeout=0.2)
        self.assertFalse(lock.acquire(timeout=0.1))


class LabjackBatchReadLockTests(unittest.TestCase):
    """``/labjack/batch_read`` takes the SAME lock as ``/invoke``, so it needed
    the same bound — otherwise a wedged ``/invoke`` would hang every
    ``/nets/state`` sweep of that LabJack, which is the original pile-up in a
    different endpoint."""

    def setUp(self):
        with hw.device_locks_meta_lock:
            hw.device_locks.clear()
        self.client = hw.app.test_client()

    def test_busy_lock_reports_nulls_instead_of_blocking(self):
        device_id = 'labjack:USB0::0x0CD5::0x0007::BUSY::INSTR'
        lock = hw._get_address_lock(device_id)
        lock.acquire()
        try:
            with patch.object(hw, '_LOCK_TIMEOUT_S', 0.3):
                start = time.monotonic()
                resp = self.client.post('/labjack/batch_read', json={
                    'device_id': device_id,
                    'nets': [{'name': 'VBAT', 'role': 'adc', 'pin': '0'}],
                })
                elapsed = time.monotonic() - start
        finally:
            lock.release()

        self.assertEqual(resp.status_code, 200)
        # Unreadable nets come back null — this endpoint's documented contract.
        self.assertEqual(resp.get_json(), {'VBAT': None})
        self.assertLess(elapsed, 5.0, 'the batch read queued on the device lock')

    def test_lock_is_released_after_a_normal_read(self):
        device_id = 'labjack:USB0::0x0CD5::0x0007::FREE::INSTR'
        resp = self.client.post('/labjack/batch_read', json={
            'device_id': device_id,
            'nets': [{'name': 'VBAT', 'role': 'adc', 'pin': '0'}],
        })
        self.assertEqual(resp.status_code, 200)
        lock = hw._get_address_lock(device_id)
        self.assertTrue(lock.acquire(timeout=0.1),
                        'batch_read leaked the device lock')
        lock.release()


if __name__ == '__main__':
    unittest.main()
