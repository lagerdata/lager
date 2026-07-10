# Copyright 2024-2026 Lager Data
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


class EnodevDetectionTests(unittest.TestCase):
    """Direct coverage of _is_enodev_error — the helper that distinguishes
    libusb's 'device disappeared' signature (after USB re-enumeration) from
    other stale-session errors, so the retry path can do a more aggressive
    cleanup."""

    def test_matches_no_such_device(self):
        self.assertTrue(hw._is_enodev_error(Exception('[Errno 19] No such device (it may have been disconnected)')))

    def test_matches_errno_19(self):
        self.assertTrue(hw._is_enodev_error(Exception('USBError: [Errno 19]')))

    def test_matches_enodev_literal(self):
        self.assertTrue(hw._is_enodev_error(Exception('VI_ERROR_NLISTENERS / ENODEV')))

    def test_matches_cannot_find(self):
        self.assertTrue(hw._is_enodev_error(Exception('Cannot find device at address USB0::...')))

    def test_does_not_match_resource_busy(self):
        self.assertFalse(hw._is_enodev_error(Exception('[Errno 16] Resource busy')))

    def test_does_not_match_stale_session(self):
        self.assertFalse(hw._is_enodev_error(Exception('Invalid VISA session: handle is no longer valid')))

    def test_does_not_match_timeout(self):
        self.assertFalse(hw._is_enodev_error(Exception('[Errno 110] Operation timed out')))


class EnodevRetryTests(unittest.TestCase):
    """Phase 3 / 0.20.0: when a cached driver hits ENODEV, the retry path must
    evict every cached device for the same address (sibling roles on the same
    physical instrument — e.g. Keithley supply + battery share one USB device)
    AND force-close the shared pyvisa pool entry even if the calling driver
    isn't normally a shared-session user. Otherwise the next call against a
    sibling reuses a stale file descriptor and ENODEV/EBUSYs again."""

    def setUp(self):
        hw.device_cache.clear()
        hw.module_cache.clear()
        with hw.device_locks_meta_lock:
            hw.device_locks.clear()
        with hw._visa_resources_meta_lock:
            hw._visa_resources.clear()
        self.client = hw.app.test_client()

    def _make_enodev_device(self, call_log, name):
        class EnodevDevice:
            def __init__(self):
                self.closed = False

            def some_method(self_inner):
                # Matches _is_enodev_error AND _is_visa_session_error.
                raise Exception('[Errno 19] No such device (it may have been disconnected)')

            def close(self_inner):
                self_inner.closed = True
                call_log.append(('close', name))
        return EnodevDevice()

    def test_enodev_evicts_sibling_cache_entries_for_same_address(self):
        call_log = []
        address = 'USB0::0x05E6::0x2281::ENODEV::INSTR'
        battery_key = ('keithley_battery', address)
        supply_key = ('keithley', address)
        unrelated_key = ('keithley', 'USB0::0xOTHER::INSTR')

        battery_device = self._make_enodev_device(call_log, 'battery')
        supply_device = self._make_enodev_device(call_log, 'supply')
        unrelated_device = self._make_enodev_device(call_log, 'unrelated')

        fake_module = FakeModule(call_log)

        hw.device_cache[battery_key] = battery_device
        hw.device_cache[supply_key] = supply_device
        hw.device_cache[unrelated_key] = unrelated_device
        hw.module_cache[battery_key] = fake_module

        resp = self.client.post('/invoke', json={
            'device': 'keithley_battery',
            'function': 'some_method',
            'args': [],
            'kwargs': {},
            'net_info': {'address': address, 'channel': 1},
        })

        self.assertEqual(resp.status_code, 200, resp.get_json())
        # battery (the calling driver) is recreated by the retry — cache holds
        # the fresh device, not the original stale one.
        self.assertIs(hw.device_cache[battery_key], fake_module.fresh_device)
        # supply (sibling on the same address) is evicted by the ENODEV cascade
        # and NOT recreated (the calling /invoke only recreates its own driver).
        self.assertNotIn(supply_key, hw.device_cache)
        # Unrelated entry on a different address must NOT be evicted.
        self.assertIn(unrelated_key, hw.device_cache)
        # Both addressed devices got close() — battery via the existing pop+close
        # path, supply via the new ENODEV cascade. Unrelated stays untouched.
        closed_names = [
            entry[1] for entry in call_log
            if isinstance(entry, tuple) and entry[0] == 'close'
        ]
        self.assertIn('battery', closed_names)
        self.assertIn('supply', closed_names)
        self.assertNotIn('unrelated', closed_names)

    def test_enodev_force_closes_shared_pool_for_non_shared_driver(self):
        """A driver not in _SHARED_VISA_DEVICE_NAMES (e.g. a Rigol) still
        benefits from the shared pool being force-closed on ENODEV — otherwise
        a future shared-session user against the same address would reuse the
        stale pool entry."""
        fake_raw = MagicMock(name='stale_raw')
        fake_rm = MagicMock(name='stale_rm')
        fake_rm.open_resource.return_value = fake_raw
        import pyvisa
        pyvisa.ResourceManager = MagicMock(return_value=fake_rm)  # type: ignore[attr-defined]

        address = 'USB0::0x1AB1::0x0E11::ENODEV::INSTR'
        # Pre-populate the shared pool as if a previous call had opened one.
        hw._get_or_open_visa_resource(address)
        self.assertIn(address, hw._visa_resources)

        call_log = []
        cache_key = ('rigol_dp800', address)
        hw.device_cache[cache_key] = self._make_enodev_device(call_log, 'rigol')
        hw.module_cache[cache_key] = FakeModule(call_log)

        resp = self.client.post('/invoke', json={
            'device': 'rigol_dp800',
            'function': 'some_method',
            'args': [],
            'kwargs': {},
            'net_info': {'address': address, 'channel': 1},
        })

        self.assertEqual(resp.status_code, 200, resp.get_json())
        # Shared pool entry was force-closed on ENODEV even though Rigol isn't
        # normally a shared-session user.
        self.assertNotIn(address, hw._visa_resources)

    def test_non_enodev_session_error_does_not_evict_siblings(self):
        """Regression guard: a plain stale-session error (not ENODEV) must NOT
        trigger the wider sibling-eviction logic — otherwise we'd reconnect
        more aggressively than necessary on every minor pyvisa hiccup."""
        call_log = []
        address = 'USB0::0x05E6::0x2281::STALE::INSTR'
        battery_key = ('keithley_battery', address)
        supply_key = ('keithley', address)

        # FakeStaleDevice raises 'Invalid VISA session' — matches the original
        # _VISA_SESSION_ERROR_KEYWORDS but NOT _ENODEV_ERROR_KEYWORDS.
        battery_device = FakeStaleDevice(call_log)
        supply_device = FakeStaleDevice(call_log)

        fake_module = FakeModule(call_log)

        hw.device_cache[battery_key] = battery_device
        hw.device_cache[supply_key] = supply_device
        hw.module_cache[battery_key] = fake_module

        resp = self.client.post('/invoke', json={
            'device': 'keithley_battery',
            'function': 'some_method',
            'args': [],
            'kwargs': {},
            'net_info': {'address': address, 'channel': 1},
        })

        self.assertEqual(resp.status_code, 200, resp.get_json())
        # Battery (the calling driver) is recreated by the existing retry path —
        # cache holds the new fresh device, original stale one was closed.
        self.assertIs(hw.device_cache[battery_key], fake_module.fresh_device)
        self.assertTrue(battery_device.closed)
        # Supply sibling is NOT evicted — the ENODEV cascade only fires on
        # ENODEV-class errors, not on plain stale-session errors like this one.
        self.assertIs(hw.device_cache[supply_key], supply_device)
        self.assertFalse(supply_device.closed)


class ChannelSyncTests(unittest.TestCase):
    """Regression: multi-channel supplies are cached once per address (cache_key
    omits the channel), so a single driver instance is shared across all
    channels. Net-level methods that act on the instance's bound channel
    (voltage/current/enable/disable/state) must be re-pointed at the requesting
    net's channel before each /invoke — otherwise a CH2/CH3 command is misrouted
    to whichever channel first created the instance (CH1), which then rejects
    e.g. any voltage above CH1's lower limit.

    Bug report: Keysight E36312A — setpoints above 6V on CH2/CH3 (25V channels)
    failed because the shared instance was still bound to CH1 (6V max)."""

    def setUp(self):
        hw.device_cache.clear()
        hw.module_cache.clear()
        with hw.device_locks_meta_lock:
            hw.device_locks.clear()
        self.client = hw.app.test_client()

    def test_sync_helper_repoints_via_set_active_channel(self):
        """_sync_device_channel calls set_active_channel(channel) when present."""
        class Dev:
            bound = None
            def set_active_channel(self, channel):
                self.bound = int(channel)
        dev = Dev()
        hw._sync_device_channel(dev, {'address': 'x', 'channel': 3})
        self.assertEqual(dev.bound, 3)

    def test_sync_helper_noop_without_hook_or_channel(self):
        """Drivers without set_active_channel, or calls without a channel, are
        left untouched (preserves behavior for single-channel devices)."""
        class NoHook:
            pass
        hw._sync_device_channel(NoHook(), {'address': 'x', 'channel': 2})  # no raise

        class Dev:
            calls = 0
            def set_active_channel(self, channel):
                self.calls += 1
        dev = Dev()
        hw._sync_device_channel(dev, {'address': 'x'})   # no channel key
        hw._sync_device_channel(dev, None)               # no net_info
        self.assertEqual(dev.calls, 0)

    def test_shared_instance_routes_each_invoke_to_requested_channel(self):
        """End-to-end through /invoke: one cached instance shared across CH1 and
        CH3 must apply each channel-less voltage() call to the channel named in
        that request's net_info, not the channel that created the instance."""
        applied = []

        class FakeSupply:
            """Mimics a shared multi-channel supply driver: voltage() acts on the
            currently-bound channel (self.channel), like the real net-level
            methods on KeysightE36000 / RigolDP800."""
            def __init__(self):
                self.channel = 1
            def set_active_channel(self, channel):
                self.channel = int(channel)
            def voltage(self, value=None):
                applied.append((self.channel, value))
                return {'ok': True}

        address = 'USB0::0x2A8D::0x1102::FAKE::INSTR'
        cache_key = ('keysight_e36000', address)
        # Instance was first created for CH1 (the bug's precondition).
        hw.device_cache[cache_key] = FakeSupply()

        # A command for CH3 must be applied to CH3, not CH1.
        resp = self.client.post('/invoke', json={
            'device': 'keysight_e36000',
            'function': 'voltage',
            'args': [],
            'kwargs': {'value': 20.0},
            'net_info': {'address': address, 'channel': 3},
        })
        self.assertEqual(resp.status_code, 200, resp.get_json())

        # A subsequent CH1 command on the same shared instance routes to CH1.
        resp = self.client.post('/invoke', json={
            'device': 'keysight_e36000',
            'function': 'voltage',
            'args': [],
            'kwargs': {'value': 5.0},
            'net_info': {'address': address, 'channel': 1},
        })
        self.assertEqual(resp.status_code, 200, resp.get_json())

        self.assertEqual(applied, [(3, 20.0), (1, 5.0)])


class DeviceIdLockTests(unittest.TestCase):
    """A net_info `device_id` provides an explicit physical-device lock key for
    devices with no VISA address (LabJack shared across GPIO/ADC/DAC/SPI/I2C,
    watt+energy on one Joulescope, etc). The cache still keeps a distinct
    driver instance per net, but the lock is shared across every net/role that
    names the same device_id — so concurrent I/O on one physical device
    serializes even across different roles/modules."""

    def setUp(self):
        hw.device_cache.clear()
        hw.module_cache.clear()
        with hw.device_locks_meta_lock:
            hw.device_locks.clear()
        self.client = hw.app.test_client()

    def _fake_module(self, value):
        dev = MagicMock(name=f'dev{value}')
        dev.read.return_value = value
        del dev.device  # not a wrapper; skip .device dereference
        mod = MagicMock()
        mod.create_device = MagicMock(return_value=dev)
        del mod.clear_resource_cache
        return mod

    def test_device_id_shared_lock_across_roles_distinct_cache(self):
        # Two roles (adc + gpio) on ONE physical LabJack: same device_id,
        # different device_name + net_info -> distinct cache entries, one lock.
        # Fake device names injected at hardware_service's first search path
        # (lager.<device>) so the test exercises only the lock logic, isolated
        # from the real *_hs adapter modules.
        mod_adc = self._fake_module(1)
        mod_gpio = self._fake_module(0)
        sys.modules['lager.fake_adc_hs'] = mod_adc
        sys.modules['lager.fake_gpio_hs'] = mod_gpio
        try:
            r1 = self.client.post('/invoke', json={
                'device': 'fake_adc_hs', 'function': 'read', 'args': [], 'kwargs': {},
                'net_info': {'device_id': 'labjack', 'pin': 0, 'name': 'adc1'},
            })
            r2 = self.client.post('/invoke', json={
                'device': 'fake_gpio_hs', 'function': 'read', 'args': [], 'kwargs': {},
                'net_info': {'device_id': 'labjack', 'pin': 1, 'name': 'gpi1'},
            })
            self.assertEqual(r1.status_code, 200, r1.get_json())
            self.assertEqual(r2.status_code, 200, r2.get_json())
            # Distinct cached driver instances (different pins) ...
            self.assertEqual(len(hw.device_cache), 2)
            # ... but a single SHARED physical-device lock keyed on device_id.
            self.assertIn(('__address__', 'labjack'), hw.device_locks)
            # No per-cache_key locks were created for these calls (they used
            # the device_id lock path, not the fallback).
            cache_key_locks = [
                k for k in hw.device_locks
                if isinstance(k, tuple) and k and k[0] != '__address__'
            ]
            self.assertEqual(cache_key_locks, [])
        finally:
            sys.modules.pop('lager.fake_adc_hs', None)
            sys.modules.pop('lager.fake_gpio_hs', None)

    def test_no_device_id_no_address_falls_back_to_cache_key_lock(self):
        mod = self._fake_module(7)
        sys.modules['lager.fake_adc_hs2'] = mod
        try:
            r = self.client.post('/invoke', json={
                'device': 'fake_adc_hs2', 'function': 'read', 'args': [], 'kwargs': {},
                'net_info': {'pin': 0, 'name': 'adc9'},
            })
            self.assertEqual(r.status_code, 200, r.get_json())
            # Fallback: lock keyed on the cache_key (device_name, net_info_hash).
            self.assertFalse(any(
                isinstance(k, tuple) and k and k[0] == '__address__'
                for k in hw.device_locks))
        finally:
            sys.modules.pop('lager.fake_adc_hs2', None)


if __name__ == '__main__':
    unittest.main()
