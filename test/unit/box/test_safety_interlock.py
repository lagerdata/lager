# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the per-net safety interlock (box/lager/safety.py).

Two things here are load-bearing and worth stating plainly:

1. ``test_fabricated_net_info_cannot_raise_the_ceiling`` is the reason the
   interlock reads limits from NetsCache rather than from the request. Anything
   on the box can POST to the hardware service, so a limit taken from the
   payload would let a caller widen its own ceiling by asking.

2. ``test_interlock_is_checked_before_the_device_is_built`` pins the check's
   position. The stale-VISA-session recovery path invokes the driver method a
   second time; a check next to either call site would leave the other open.
"""

import os
import sys
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

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

import lager.hardware_service as hw  # noqa: E402
import lager.safety as safety  # noqa: E402


# A 3.7 V cell on a supply that can deliver 30 V -- the case the interlock exists for.
VBAT = {
    'name': 'vbat',
    'role': 'power-supply',
    'instrument': 'Rigol_DP832',
    'address': 'USB0::0x1AB1::0x0E11::FAKE::INSTR',
    'channel': 1,
    'safety_limits': {'max_voltage': 4.3, 'max_current': 2.0},
}

# Same bench, no limits declared. Must stay completely unrestricted.
VBUS = {
    'name': 'vbus',
    'role': 'power-supply',
    'instrument': 'Rigol_DP832',
    'address': 'USB0::0x1AB1::0x0E11::OTHER::INSTR',
    'channel': 2,
}


class FakeNetsCache:
    """Stands in for lager.cache.NetsCache with a fixed record set."""

    def __init__(self, records):
        self._records = list(records)

    def get_nets(self):
        return list(self._records)

    def find_by_name(self, name):
        for record in self._records:
            if record.get('name') == name:
                return record
        return None


def _use_nets(*records):
    """Patch the interlock's view of saved nets."""
    return patch.object(safety, 'get_nets_cache', lambda: FakeNetsCache(records))


class SafetyCheckTests(unittest.TestCase):
    """Direct coverage of safety.check -- no Flask, no driver."""

    def setUp(self):
        safety.reset_rate_state()

    def test_setpoint_over_the_ceiling_is_refused(self):
        with _use_nets(VBAT):
            with self.assertRaises(safety.SafetyViolation) as ctx:
                safety.check('vbat', 'voltage', [], {'value': 24.0})
        self.assertEqual(ctx.exception.limit_kind, 'max_voltage')
        self.assertEqual(ctx.exception.limit_value, 4.3)
        self.assertEqual(ctx.exception.requested, 24.0)

    def test_setpoint_under_the_ceiling_passes(self):
        with _use_nets(VBAT):
            safety.check('vbat', 'voltage', [], {'value': 3.7})

    def test_setpoint_exactly_at_the_ceiling_passes(self):
        """The limit is a ceiling, not an exclusive bound -- 4.3 on a 4.3 limit
        is the documented maximum, and refusing it would make the number lie."""
        with _use_nets(VBAT):
            safety.check('vbat', 'voltage', [], {'value': 4.3})

    def test_positional_setpoint_is_checked_too(self):
        """Callers use voltage(24.0) as well as voltage(value=24.0)."""
        with _use_nets(VBAT):
            with self.assertRaises(safety.SafetyViolation):
                safety.check('vbat', 'voltage', [24.0], {})

    def test_negative_setpoint_is_checked_by_magnitude(self):
        """A two-quadrant supply sources negative voltage, and -24 V is exactly
        as damaging to a 3.7 V cell as +24 V."""
        with _use_nets(VBAT):
            with self.assertRaises(safety.SafetyViolation):
                safety.check('vbat', 'voltage', [], {'value': -24.0})

    def test_current_limit_is_separate_from_voltage(self):
        with _use_nets(VBAT):
            safety.check('vbat', 'current', [], {'value': 1.5})
            with self.assertRaises(safety.SafetyViolation) as ctx:
                safety.check('vbat', 'current', [], {'value': 5.0})
        self.assertEqual(ctx.exception.limit_kind, 'max_current')

    def test_protection_setpoint_uses_the_matching_limit(self):
        """ovp() sets the over-voltage trip; letting it exceed max_voltage
        would defeat the ceiling by raising the instrument's own guard."""
        with _use_nets(VBAT):
            with self.assertRaises(safety.SafetyViolation) as ctx:
                safety.check('vbat', 'ovp', [], {'value': 30.0})
        self.assertEqual(ctx.exception.limit_kind, 'max_voltage')

    def test_inline_protection_trip_is_checked(self):
        """voltage(value=3.7, ovp=30.0) sets a 30 V trip while the setpoint
        itself looks harmless. Checking only the primary value would leave the
        instrument's own guard raised above the net's ceiling."""
        with _use_nets(VBAT):
            with self.assertRaises(safety.SafetyViolation) as ctx:
                safety.check('vbat', 'voltage', [], {'value': 3.7, 'ovp': 30.0})
        self.assertEqual(ctx.exception.limit_kind, 'max_voltage')
        self.assertEqual(ctx.exception.requested, 30.0)

    def test_inline_current_trip_is_checked(self):
        with _use_nets(VBAT):
            with self.assertRaises(safety.SafetyViolation) as ctx:
                safety.check('vbat', 'voltage', [], {'value': 3.7, 'ocp': 9.0})
        self.assertEqual(ctx.exception.limit_kind, 'max_current')

    def test_inline_trips_within_limits_pass(self):
        with _use_nets(VBAT):
            safety.check('vbat', 'voltage', [], {'value': 3.7, 'ovp': 4.2, 'ocp': 1.0})

    def test_read_call_with_no_setpoint_passes(self):
        """voltage() with no argument queries rather than sets."""
        with _use_nets(VBAT):
            safety.check('vbat', 'voltage', [], {})

    def test_net_without_limits_is_unrestricted(self):
        with _use_nets(VBAT, VBUS):
            safety.check('vbus', 'voltage', [], {'value': 30.0})

    def test_unknown_net_is_unrestricted(self):
        with _use_nets(VBAT):
            safety.check('nonexistent', 'voltage', [], {'value': 30.0})

    def test_unguarded_function_passes_on_a_limited_net(self):
        """state() reads; it cannot damage anything and must not be throttled."""
        with _use_nets(VBAT):
            for _ in range(500):
                safety.check('vbat', 'state', [], {})

    def test_unguarded_role_is_ignored(self):
        """A UART net has no voltage ceiling to enforce."""
        uart = {'name': 'console', 'role': 'uart', 'safety_limits': {'max_voltage': 1.0}}
        with _use_nets(uart):
            safety.check('console', 'voltage', [], {'value': 99.0})


class DriverMethodNameTests(unittest.TestCase):
    """The table is keyed on driver method names, not dispatcher function names.

    These two layers diverge -- the battery dispatcher's `set_ovp` calls
    `drv.ovp`, and the eload dispatcher's `set_constant_current` calls
    `drv.set_current`. Keying on the dispatcher names would guard nothing, so
    these tests pin the names that actually cross the wire.
    """

    def setUp(self):
        safety.reset_rate_state()

    def test_battery_cell_profile_voltages_are_guarded(self):
        """A battery simulator has no `voltage` setter. The cell profile is
        described by open-circuit and full/empty terminal voltages, and every
        one of them can present a damaging potential to the DUT."""
        cell = {
            'name': 'cell', 'role': 'battery', 'instrument': 'Keithley_2281S',
            'address': 'USB0::0x05E6::0x2281::FAKE::INSTR', 'channel': 1,
            'safety_limits': {'max_voltage': 4.3, 'max_current': 2.0},
        }
        with _use_nets(cell):
            for method in ('voc', 'voltage_full', 'voltage_empty', 'ovp'):
                with self.assertRaises(safety.SafetyViolation, msg=method):
                    safety.check('cell', method, [], {'value': 24.0})
            for method in ('current_limit', 'ocp'):
                with self.assertRaises(safety.SafetyViolation, msg=method):
                    safety.check('cell', method, [], {'value': 9.0})

    def test_battery_capacity_is_not_a_voltage_or_current(self):
        """set_capacity carries amp-hours. It is not a setpoint a V/I ceiling
        applies to, and guarding it would refuse legitimate values."""
        cell = {
            'name': 'cell', 'role': 'battery',
            'safety_limits': {'max_voltage': 4.3, 'max_current': 2.0},
        }
        with _use_nets(cell):
            safety.check('cell', 'capacity', [], {'value': 5000.0})

    def test_eload_positional_setpoint_is_guarded(self):
        """Electronic loads take the setpoint positionally: set_current(9.0)."""
        load = {
            'name': 'sink', 'role': 'eload', 'instrument': 'Rigol_DL3021',
            'safety_limits': {'max_current': 2.0, 'max_voltage': 5.0},
        }
        with _use_nets(load):
            safety.check('sink', 'set_current', [1.5], {})
            with self.assertRaises(safety.SafetyViolation) as ctx:
                safety.check('sink', 'set_current', [9.0], {})
        self.assertEqual(ctx.exception.limit_kind, 'max_current')

    def test_solar_role_declares_no_ceiling(self):
        """The solar driver surface is irradiance, resistance, and MPP
        readbacks -- there is no voltage or current setpoint for a ceiling to
        apply to. If a setpoint is ever added, this test should fail and the
        table should gain an entry."""
        panel = {
            'name': 'array', 'role': 'solar',
            'safety_limits': {'max_voltage': 5.0},
        }
        with _use_nets(panel):
            safety.check('array', 'irradiance', [], {'value': 9999.0})


class RateCapTests(unittest.TestCase):
    """The call-rate cap. Clock is driven explicitly -- never wall-clock."""

    def setUp(self):
        safety.reset_rate_state()

    def test_runaway_loop_is_stopped(self):
        with _use_nets(VBAT), patch.object(safety.time, 'monotonic', return_value=1000.0):
            for _ in range(safety._rate_max_calls()):
                safety.check('vbat', 'enable', [], {})
            with self.assertRaises(safety.RateLimitExceeded) as ctx:
                safety.check('vbat', 'enable', [], {})
        self.assertEqual(ctx.exception.net_name, 'vbat')
        self.assertEqual(ctx.exception.function_name, 'enable')

    def test_window_rolls_and_allows_again(self):
        clock = {'now': 1000.0}
        with _use_nets(VBAT), patch.object(safety.time, 'monotonic', side_effect=lambda: clock['now']):
            for _ in range(safety._rate_max_calls()):
                safety.check('vbat', 'enable', [], {})
            with self.assertRaises(safety.RateLimitExceeded):
                safety.check('vbat', 'enable', [], {})
            clock['now'] += safety._rate_window_seconds() + 1.0
            safety.check('vbat', 'enable', [], {})

    def test_cap_is_per_function_not_per_net(self):
        """A voltage sweep must not spend the budget that a relay cycle needs."""
        with _use_nets(VBAT), patch.object(safety.time, 'monotonic', return_value=1000.0):
            for _ in range(safety._rate_max_calls()):
                safety.check('vbat', 'enable', [], {})
            safety.check('vbat', 'disable', [], {})

    def test_unlimited_net_is_not_rate_capped(self):
        """The cap is a property of having limits, not a global throttle."""
        with _use_nets(VBAT, VBUS), patch.object(safety.time, 'monotonic', return_value=1000.0):
            for _ in range(safety._rate_max_calls() * 3):
                safety.check('vbus', 'enable', [], {})

    def test_value_violation_is_reported_before_a_rate_violation(self):
        """When both would trip, the dangerous setpoint is the useful message."""
        with _use_nets(VBAT), patch.object(safety.time, 'monotonic', return_value=1000.0):
            for _ in range(safety._rate_max_calls() + 5):
                try:
                    safety.check('vbat', 'voltage', [], {'value': 3.0})
                except safety.RateLimitExceeded:
                    break
            with self.assertRaises(safety.SafetyViolation):
                safety.check('vbat', 'voltage', [], {'value': 24.0})


class NetIdentificationTests(unittest.TestCase):
    """How a request is mapped to a saved net, and what happens when it can't be."""

    def setUp(self):
        safety.reset_rate_state()

    def test_explicit_name_is_used(self):
        with _use_nets(VBAT):
            self.assertEqual(safety.resolve_net_name({'name': 'vbat'}), 'vbat')

    def test_address_and_channel_identify_a_net_without_a_name(self):
        """Callers built before the name was carried on the wire still resolve."""
        with _use_nets(VBAT, VBUS):
            resolved = safety.resolve_net_name(
                {'address': VBAT['address'], 'channel': 1}
            )
        self.assertEqual(resolved, 'vbat')

    def test_guarded_call_without_identification_is_refused_when_limits_exist(self):
        """Fail closed: a box with limits configured must not accept a guarded
        command it cannot attribute to a net."""
        with _use_nets(VBAT):
            with self.assertRaises(safety.UnidentifiedNet):
                safety.check_invocation({}, 'voltage', [], {'value': 24.0})

    def test_interlock_is_dormant_when_no_limits_are_configured_anywhere(self):
        """A bench that has never configured a limit keeps working untouched --
        adding the first limit is what turns enforcement on, not upgrading."""
        with _use_nets(VBUS):
            safety.check_invocation({}, 'voltage', [], {'value': 24.0})

    def test_unguarded_function_is_allowed_without_identification(self):
        with _use_nets(VBAT):
            safety.check_invocation({}, 'state', [], {})


class DestructiveOperationTests(unittest.TestCase):
    """Erase and flash: permitted by default, refusable per net.

    Deliberately not a global env flag. Flashing is what a hardware test does,
    so defaulting it off would break every existing bench on upgrade for no
    gain on the benches nobody was worried about.
    """

    def setUp(self):
        safety.reset_rate_state()

    def test_flash_is_permitted_by_default(self):
        dut = {'name': 'dut', 'role': 'debug', 'channel': 'nrf52840'}
        with _use_nets(dut):
            safety.check_destructive({'name': 'dut'}, 'flash')

    def test_flash_is_permitted_when_limits_exist_but_do_not_forbid_it(self):
        """A net with a voltage ceiling is still flashable -- the two controls
        are independent."""
        dut = {'name': 'dut', 'role': 'debug', 'safety_limits': {'max_voltage': 3.3}}
        with _use_nets(dut):
            safety.check_destructive({'name': 'dut'}, 'flash')

    def test_net_can_opt_out_of_destructive_operations(self):
        """A golden sample, or a part whose bootloader cannot be recovered."""
        golden = {
            'name': 'golden', 'role': 'debug',
            'safety_limits': {'allow_destructive': False},
        }
        with _use_nets(golden):
            for operation in ('flash', 'erase'):
                with self.assertRaises(safety.DestructiveOperationRefused, msg=operation) as ctx:
                    safety.check_destructive({'name': 'golden'}, operation)
                self.assertEqual(ctx.exception.operation, operation)
                self.assertEqual(ctx.exception.net_name, 'golden')

    def test_explicit_true_is_permitted(self):
        dut = {'name': 'dut', 'role': 'debug', 'safety_limits': {'allow_destructive': True}}
        with _use_nets(dut):
            safety.check_destructive({'name': 'dut'}, 'flash')

    def test_unknown_net_is_permitted(self):
        with _use_nets():
            safety.check_destructive({'name': 'mystery'}, 'flash')


class InvokeEndpointTests(unittest.TestCase):
    """End-to-end through the real /invoke handler, with a fake driver module."""

    def setUp(self):
        safety.reset_rate_state()
        self.device = MagicMock(name='supply_device')
        self.device.voltage.return_value = None
        del self.device.device
        self.module = MagicMock(name='fake_supply_module')
        self.module.create_device = MagicMock(return_value=self.device)
        del self.module.clear_resource_cache
        sys.modules['lager.power.supply.rigol_dp800'] = self.module
        hw.device_cache.clear()

    def tearDown(self):
        sys.modules.pop('lager.power.supply.rigol_dp800', None)
        hw.device_cache.clear()

    def _post(self, net_info, function='voltage', kwargs=None):
        return hw.app.test_client().post('/invoke', json={
            'device': 'rigol_dp800',
            'function': function,
            'args': [],
            'kwargs': kwargs if kwargs is not None else {'value': 24.0},
            'net_info': net_info,
        })

    def test_over_limit_setpoint_is_refused_with_403(self):
        with _use_nets(VBAT):
            resp = self._post({'name': 'vbat', 'address': VBAT['address'], 'channel': 1})
        self.assertEqual(resp.status_code, 403, resp.get_json())
        body = resp.get_json()
        self.assertEqual(body['reason'], 'safety_limit_exceeded')
        self.assertEqual(body['net'], 'vbat')
        self.assertEqual(body['limit'], 4.3)
        self.assertEqual(body['requested'], 24.0)

    def test_interlock_is_checked_before_the_device_is_built(self):
        """A refused command must never reach the instrument. create_device is
        the first thing that touches hardware, so it must not have been called.

        This also covers the stale-session retry path: the check sits ahead of
        the try/except that owns the second invocation, so there is no second
        call site to guard."""
        with _use_nets(VBAT):
            self._post({'name': 'vbat', 'address': VBAT['address'], 'channel': 1})
        self.module.create_device.assert_not_called()
        self.device.voltage.assert_not_called()

    def test_fabricated_net_info_cannot_raise_the_ceiling(self):
        """The payload is attacker-controlled on a shared box. Limits are read
        from the saved-net record, so claiming a higher ceiling in the request
        changes nothing."""
        with _use_nets(VBAT):
            resp = self._post({
                'name': 'vbat',
                'address': VBAT['address'],
                'channel': 1,
                'safety_limits': {'max_voltage': 999.0},
            })
        self.assertEqual(resp.status_code, 403, resp.get_json())
        self.assertEqual(resp.get_json()['limit'], 4.3)
        self.module.create_device.assert_not_called()

    def test_dropping_the_name_does_not_evade_the_ceiling(self):
        """Omitting the name falls back to address matching rather than
        silently disabling the check."""
        with _use_nets(VBAT):
            resp = self._post({'address': VBAT['address'], 'channel': 1})
        self.assertEqual(resp.status_code, 403, resp.get_json())
        self.assertEqual(resp.get_json()['net'], 'vbat')

    def test_permitted_setpoint_reaches_the_driver(self):
        with _use_nets(VBAT):
            resp = self._post(
                {'name': 'vbat', 'address': VBAT['address'], 'channel': 1},
                kwargs={'value': 3.7},
            )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.device.voltage.assert_called_once_with(value=3.7)

    def test_unlimited_net_reaches_the_driver(self):
        with _use_nets(VBAT, VBUS):
            resp = self._post({'name': 'vbus', 'address': VBUS['address'], 'channel': 2})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.device.voltage.assert_called_once_with(value=24.0)

    def test_rate_cap_surfaces_as_403(self):
        with _use_nets(VBAT), patch.object(safety.time, 'monotonic', return_value=1000.0):
            for _ in range(safety._rate_max_calls()):
                self._post({'name': 'vbat'}, function='enable', kwargs={})
            resp = self._post({'name': 'vbat'}, function='enable', kwargs={})
        self.assertEqual(resp.status_code, 403, resp.get_json())
        self.assertEqual(resp.get_json()['reason'], 'safety_rate_limit_exceeded')


if __name__ == '__main__':
    unittest.main()
