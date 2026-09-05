# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the PicoScope Net mapper (box/lager/nets/mappers/picoscope.py).

This is the object a `lager python` script gets back from
``Net.get('scope1', type=NetType.Analog)``. It used to be ``PassThroughMapper``
pointed at a backend module named ``picoscope_2000``, which does not exist, so
every call raised "Hardware module not found" -- the Python API for a PicoScope
did not work at all. These tests cover the two things that regression needs:

  - the mapper reaches the driver, with the net's own channel as the default
  - features a PicoScope does not have raise, naming the gap, rather than
    returning a plausible-looking zero

The device is a mock: what is being tested is the mapping, and the driver
underneath it is exercised against real hardware separately.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted: str) -> None:
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


for _dep in ('pyvisa', 'pyvisa.constants', 'usb', 'usb.util', 'usb.core',
             'pigpio', 'labjack', 'labjack.ljm', 'serial', 'serial.tools',
             'serial.tools.list_ports', 'flask_socketio', 'numpy'):
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.measurement.scope.picoscope import UnsupportedScopeFeature  # noqa: E402
from lager.nets.mappers.picoscope import PicoScopeAnalogMapper  # noqa: E402


class _FakeNet:
    """Just the attributes the mapper reads off a Net."""

    def __init__(self, channel='B'):
        self.channel = channel
        self.name = 'scope1'


def _mapper(channel='B'):
    device = MagicMock()
    return PicoScopeAnalogMapper(_FakeNet(channel), device), device


class AcquisitionNamingTests(unittest.TestCase):
    """The Rigol mapper's capture verbs, so a script reads the same either way."""

    def test_start_capture_runs_the_scope(self):
        mapper, device = _mapper()
        mapper.start_capture()
        device.run.assert_called_once_with()

    def test_stop_capture_stops_the_scope(self):
        mapper, device = _mapper()
        mapper.stop_capture()
        device.stop.assert_called_once_with()

    def test_start_single_capture_arms_one_shot(self):
        mapper, device = _mapper()
        mapper.start_single_capture()
        device.single.assert_called_once_with()

    def test_force_trigger_triggers_now(self):
        mapper, device = _mapper()
        mapper.force_trigger()
        device.trigger_force.assert_called_once_with()

    def test_an_unmapped_method_still_reaches_the_driver(self):
        # The mapper adds a surface; it must not hide the one underneath.
        mapper, device = _mapper()
        mapper.get_sample_rate()
        device.get_sample_rate.assert_called_once_with()


class ChannelDefaultingTests(unittest.TestCase):
    """A scope net is wired to one channel, and that is the default."""

    def test_measurements_use_the_nets_channel(self):
        mapper, device = _mapper(channel='B')
        mapper.measurement.voltage_peak_to_peak()
        device.measure.assert_called_once_with('vpp', 'B')

    def test_scaling_uses_the_nets_channel(self):
        mapper, device = _mapper(channel='B')
        mapper.trace_settings.set_volts_per_div(0.5)
        device.set_channel_scale.assert_called_once_with(0.5, 'B')

    def test_the_trigger_source_defaults_to_the_nets_channel(self):
        mapper, device = _mapper(channel='B')
        mapper.trigger_settings.edge.set_source()
        device.set_trigger_source.assert_called_once_with('B')

    def test_an_explicit_trigger_source_wins(self):
        mapper, device = _mapper(channel='B')
        mapper.trigger_settings.edge.set_source('A')
        device.set_trigger_source.assert_called_once_with('A')

    def test_a_net_without_a_channel_does_not_blow_up(self):
        # Not every net carries a pin; the driver's own default applies.
        net = _FakeNet()
        del net.channel
        device = MagicMock()
        mapper = PicoScopeAnalogMapper(net, device)
        mapper.measurement.voltage_max()
        device.measure.assert_called_once_with('vmax', None)


class MeasurementMappingTests(unittest.TestCase):
    """Documented Rigol names map onto the daemon's measurement names."""

    CASES = [
        ('voltage_max', 'vmax'),
        ('voltage_min', 'vmin'),
        ('voltage_peak_to_peak', 'vpp'),
        ('voltage_average', 'vavg'),
        ('voltage_rms', 'vrms'),
        ('voltage_overshoot', 'overshoot'),
        ('frequency', 'frequency'),
        ('period', 'period'),
        ('rise_time', 'rise_time'),
        ('fall_time', 'fall_time'),
        ('pulse_width_positive', 'pulse_width_pos'),
        ('pulse_width_negative', 'pulse_width_neg'),
        ('duty_cycle_positive', 'duty_cycle_pos'),
        ('duty_cycle_negative', 'duty_cycle_neg'),
    ]

    def test_every_supported_measurement_maps_to_a_daemon_name(self):
        for method, expected in self.CASES:
            with self.subTest(method=method):
                mapper, device = _mapper(channel='A')
                getattr(mapper.measurement, method)()
                device.measure.assert_called_once_with(expected, 'A')

    def test_the_rigols_display_arguments_are_accepted_and_ignored(self):
        # A PicoScope has no screen to draw a reading on, but a script
        # written for a Rigol should not have to strip the argument.
        mapper, device = _mapper(channel='A')
        mapper.measurement.voltage_max(display=True, measurement_cursor=True)
        device.measure.assert_called_once_with('vmax', 'A')

    def test_all_reads_the_whole_set_from_one_capture(self):
        mapper, device = _mapper(channel='A')
        device.measure_all.return_value = {'vpp': 1.0}
        self.assertEqual(mapper.measurement.all(), {'vpp': 1.0})
        device.measure_all.assert_called_once_with('A')


class TriggerModeTests(unittest.TestCase):
    def test_the_three_modes_map_to_capture_modes(self):
        for method, expected in [('set_mode_auto', 'auto'),
                                 ('set_mode_normal', 'normal'),
                                 ('set_mode_single', 'single')]:
            with self.subTest(method=method):
                mapper, device = _mapper()
                getattr(mapper.trigger_settings, method)()
                device.set_capture_mode.assert_called_once_with(expected)

    def test_slopes_map_to_the_daemons_tokens(self):
        for method, expected in [('set_slope_rising', 'rising'),
                                 ('set_slope_falling', 'falling'),
                                 ('set_slope_both', 'either')]:
            with self.subTest(method=method):
                mapper, device = _mapper()
                getattr(mapper.trigger_settings.edge, method)()
                device.set_trigger_slope.assert_called_once_with(expected)

    def test_status_reports_readiness(self):
        mapper, device = _mapper()
        device.is_ready.return_value = True
        self.assertEqual(mapper.trigger_settings.get_status(), 'READY')
        device.is_ready.return_value = False
        self.assertEqual(mapper.trigger_settings.get_status(), 'WAIT')


class UnsupportedFeatureTests(unittest.TestCase):
    """A missing feature has to say so, not return a misleading number."""

    def test_cursors_raise_because_there_is_no_screen(self):
        mapper, _ = _mapper()
        for method in ('set_a', 'get_a', 'move_b', 'x_delta', 'frequency', 'hide'):
            with self.subTest(method=method):
                with self.assertRaises(UnsupportedScopeFeature) as caught:
                    getattr(mapper.cursor, method)(1.0)
                self.assertIn('cursor', str(caught.exception))

    def test_uncomputed_measurements_raise(self):
        mapper, _ = _mapper()
        for method in ('variance', 'waveform_area', 'positive_edge_count',
                       'voltage_flat_top', 'positive_slew_rate',
                       'time_at_voltage_max', 'voltage_threshold_mid'):
            with self.subTest(method=method):
                with self.assertRaises(UnsupportedScopeFeature):
                    getattr(mapper.measurement, method)()

    def test_two_channel_measurements_explain_why_they_are_missing(self):
        mapper, _ = _mapper()
        with self.assertRaises(UnsupportedScopeFeature) as caught:
            mapper.measurement.delay_rising_rising_edge()
        self.assertIn('two-channel', str(caught.exception))

    def test_trigger_coupling_points_at_the_channel_setting(self):
        mapper, _ = _mapper()
        with self.assertRaises(UnsupportedScopeFeature) as caught:
            mapper.trigger_settings.set_coupling_DC()
        self.assertIn('set_channel_coupling', str(caught.exception))

    def test_an_unsupported_measurement_never_reaches_the_device(self):
        # The point of raising is to avoid a wrong number; a stray call that
        # returned a MagicMock would defeat it.
        mapper, device = _mapper()
        with self.assertRaises(UnsupportedScopeFeature):
            mapper.measurement.variance()
        device.measure.assert_not_called()
        device.measure_all.assert_not_called()


class RigolParityTests(unittest.TestCase):
    """The groups a script written against the Rigol reaches for."""

    def test_the_documented_attribute_groups_all_exist(self):
        mapper, _ = _mapper()
        for group in ('measurement', 'trigger_settings', 'trace_settings', 'cursor'):
            with self.subTest(group=group):
                self.assertTrue(hasattr(mapper, group))

    def test_the_edge_trigger_subgroup_exists(self):
        mapper, _ = _mapper()
        self.assertTrue(hasattr(mapper.trigger_settings, 'edge'))

    def test_every_method_the_docs_promise_on_trace_settings_exists(self):
        mapper, _ = _mapper()
        for method in ('set_volts_per_div', 'get_volts_per_div',
                       'set_volt_offset', 'get_volt_offset',
                       'set_time_per_div', 'get_time_per_div',
                       'set_time_offset', 'get_time_offset'):
            with self.subTest(method=method):
                self.assertTrue(callable(getattr(mapper.trace_settings, method)))


if __name__ == '__main__':
    unittest.main()
