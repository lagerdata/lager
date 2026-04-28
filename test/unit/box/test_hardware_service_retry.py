# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""
Unit test for the close-then-recreate retry path in box/lager/hardware_service.py.

Regression guard for v0.16.7's "Concurrent battery TUI + CLI on Keithley"
known limitation. Without _close_device on the popped cache entry, the new
pyvisa session opened by module.create_device() collides with the still-live
USB claim from the popped instance and fails with [Errno 16] Resource busy.
"""

import os
import sys
import types
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

sys.modules['simplejson'] = sys.modules['json']  # type: ignore[assignment]

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

import lager.hardware_service as hw  # noqa: E402


class FakeStaleDevice:
    """A cached device whose method raises a stale-session error and whose
    close() records being called. Used to verify the retry path closes the
    old instance before module.create_device() opens a new pyvisa session."""

    def __init__(self, call_log):
        self._call_log = call_log
        self.closed = False

    def some_method(self):
        # Error message matches _VISA_SESSION_ERROR_KEYWORDS ('invalid', 'session')
        # so _is_visa_session_error() classifies it as a stale-session signal.
        raise Exception("Invalid VISA session: handle is no longer valid")

    def close(self):
        self.closed = True
        self._call_log.append('close')


class FakeFreshDevice:
    """Replacement device returned by module.create_device(net_info) on retry."""

    def __init__(self, call_log):
        self._call_log = call_log

    def some_method(self):
        return {'ok': True, 'value': 42}


class FakeModule:
    """Stands in for a hardware driver module (e.g. lager.power.battery.keithley).
    Records calls to create_device() so we can assert ordering."""

    def __init__(self, call_log):
        self._call_log = call_log
        self.fresh_device = None

    def create_device(self, net_info):
        self._call_log.append('create_device')
        self.fresh_device = FakeFreshDevice(self._call_log)
        return self.fresh_device


class HardwareServiceRetryTests(unittest.TestCase):

    def setUp(self):
        # Reset module-level state before each test.
        hw.device_cache.clear()
        hw.module_cache.clear()
        with hw.device_locks_meta_lock:
            hw.device_locks.clear()
        with hw._visa_resources_meta_lock:
            hw._visa_resources.clear()
        self.client = hw.app.test_client()

    def test_retry_path_closes_old_device_before_recreating(self):
        """Regression: when /invoke retries on a stale-session error, the popped
        cache entry must be closed BEFORE module.create_device() runs. Otherwise
        the still-live pyvisa session keeps the USB device claimed and the new
        open_resource() fails with [Errno 16] Resource busy."""
        call_log = []
        net_info = {'address': 'USB0::0xFAKE::INSTR', 'channel': 1}
        cache_key = ('fake_device', 'USB0::0xFAKE::INSTR')

        # Pre-populate the cache as if a previous /invoke had created the device.
        old_device = FakeStaleDevice(call_log)
        fake_module = FakeModule(call_log)
        hw.device_cache[cache_key] = old_device
        hw.module_cache[cache_key] = fake_module

        resp = self.client.post('/invoke', json={
            'device': 'fake_device',
            'function': 'some_method',
            'args': [],
            'kwargs': {},
            'net_info': net_info,
        })

        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json(), {'ok': True, 'value': 42})

        # The critical assertion: close fired before create_device on the same
        # cache_key. If someone removes _close_device from the retry path the
        # log will be ['create_device'] and this test will fail.
        self.assertEqual(call_log, ['close', 'create_device'])

        # Sanity checks: old device closed, new device cached.
        self.assertTrue(old_device.closed)
        self.assertIs(hw.device_cache[cache_key], fake_module.fresh_device)


class SharedVisaResourceTests(unittest.TestCase):
    """Phase 2 / v0.16.8: Keithley 2281S supply + battery share one pyvisa session
    when configured on the same physical USB device."""

    def setUp(self):
        hw.device_cache.clear()
        hw.module_cache.clear()
        with hw.device_locks_meta_lock:
            hw.device_locks.clear()
        with hw._visa_resources_meta_lock:
            hw._visa_resources.clear()

    def test_get_or_open_returns_same_session_for_same_address(self):
        """Two `_get_or_open_visa_resource(addr)` calls for the same address must
        return the SAME (rm, raw) tuple. Otherwise a sibling driver would open
        its own pyvisa session and re-introduce the dual-role Resource busy
        bug."""
        fake_raw = MagicMock()
        fake_rm = MagicMock()
        fake_rm.open_resource.return_value = fake_raw

        # Stub pyvisa.ResourceManager() to return our fake.
        import pyvisa  # already stubbed at top of file as a MagicMock module
        pyvisa.ResourceManager = MagicMock(return_value=fake_rm)  # type: ignore[attr-defined]

        addr = 'USB0::0x05E6::0x2281::FAKE::INSTR'
        rm1, raw1 = hw._get_or_open_visa_resource(addr)
        rm2, raw2 = hw._get_or_open_visa_resource(addr)

        self.assertIs(rm1, rm2)
        self.assertIs(raw1, raw2)
        # open_resource called only ONCE despite two _get_or_open calls.
        fake_rm.open_resource.assert_called_once_with(addr)

    def test_close_visa_resource_removes_entry(self):
        fake_raw = MagicMock()
        fake_rm = MagicMock()
        fake_rm.open_resource.return_value = fake_raw
        import pyvisa
        pyvisa.ResourceManager = MagicMock(return_value=fake_rm)  # type: ignore[attr-defined]

        addr = 'USB0::0xCAFE::INSTR'
        hw._get_or_open_visa_resource(addr)
        self.assertIn(addr, hw._visa_resources)

        hw._close_visa_resource(addr)
        self.assertNotIn(addr, hw._visa_resources)
        fake_raw.close.assert_called_once()

    def test_keithley_create_device_receives_shared_raw_resource(self):
        """When /invoke is called for a Keithley device with an address, the
        module's create_device must be called with raw_resource=<shared raw>.
        Regression guard against the Bug A fix being undone."""
        fake_raw = MagicMock(name='shared_raw_session')
        fake_rm = MagicMock(name='shared_rm')
        fake_rm.open_resource.return_value = fake_raw
        import pyvisa
        pyvisa.ResourceManager = MagicMock(return_value=fake_rm)  # type: ignore[attr-defined]

        # Build a fake `lager.power.battery.keithley_battery` module.
        result_device = MagicMock(name='keithley_battery_device')
        result_device.some_method.return_value = 'ok'

        fake_module = MagicMock(name='fake_keithley_battery_module')
        fake_module.create_device = MagicMock(return_value=result_device)
        # Don't pretend to be a wrapper — the post-create dereferencing
        # `if hasattr(device, 'device') ...` would otherwise descend.
        del result_device.device

        # Inject the fake module into sys.modules so importlib.import_module
        # finds it on the lager.power.battery.keithley_battery path.
        sys.modules['lager.power.battery.keithley_battery'] = fake_module

        try:
            client = hw.app.test_client()
            resp = client.post('/invoke', json={
                'device': 'keithley_battery',
                'function': 'some_method',
                'args': [],
                'kwargs': {},
                'net_info': {'address': 'USB0::0x05E6::0x2281::SHARED::INSTR', 'channel': 1},
            })
            self.assertEqual(resp.status_code, 200, resp.get_json())

            # The critical assertion: create_device was called with raw_resource=
            # pointing at the shared raw session.
            fake_module.create_device.assert_called_once()
            call_args = fake_module.create_device.call_args
            self.assertEqual(call_args.kwargs.get('raw_resource'), fake_raw,
                             "Keithley create_device must receive raw_resource= "
                             "from hardware_service's shared visa cache so the "
                             "supply and battery drivers don't open competing "
                             "pyvisa sessions on the same USB device.")
        finally:
            sys.modules.pop('lager.power.battery.keithley_battery', None)

    def test_non_shared_device_does_not_get_raw_resource(self):
        """Single-role drivers (Rigol, Keysight, etc.) must continue to use
        the legacy per-driver-opens-its-own-session path. raw_resource= must
        NOT be passed to their create_device."""
        result_device = MagicMock(name='rigol_device')
        result_device.some_method.return_value = 'ok'
        del result_device.device

        fake_module = MagicMock(name='fake_rigol_module')
        fake_module.create_device = MagicMock(return_value=result_device)
        # Don't satisfy clear_resource_cache attr lookup unexpectedly
        del fake_module.clear_resource_cache

        sys.modules['lager.power.supply.rigol_dp800'] = fake_module
        try:
            client = hw.app.test_client()
            resp = client.post('/invoke', json={
                'device': 'rigol_dp800',
                'function': 'some_method',
                'args': [],
                'kwargs': {},
                'net_info': {'address': 'USB0::0x1AB1::0x0E11::FAKE::INSTR', 'channel': 1},
            })
            self.assertEqual(resp.status_code, 200, resp.get_json())

            # raw_resource must NOT be in the kwargs — the shared-resource path
            # is gated to _SHARED_VISA_DEVICE_NAMES.
            fake_module.create_device.assert_called_once()
            call_args = fake_module.create_device.call_args
            self.assertNotIn('raw_resource', call_args.kwargs)
        finally:
            sys.modules.pop('lager.power.supply.rigol_dp800', None)


if __name__ == '__main__':
    unittest.main()
