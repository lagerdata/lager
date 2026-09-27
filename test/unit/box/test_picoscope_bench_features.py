# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The PicoScope's bench-scope settings: acquisition, holdoff, roll, display
and the spectrum (box/lager/measurement/scope/picoscope.py).

The daemon does the work for all of these, so the driver methods are thin --
which leaves their validation as the part worth pinning. The daemon stores
display settings without looking at them, so a bad value let through here
is not refused anywhere: every open page is handed it and draws nonsense.
"""

import math
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
             'serial', 'serial.tools', 'serial.tools.list_ports'):
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

import numpy as np  # noqa: E402

from lager.measurement.scope import daemon_client, picoscope  # noqa: E402

_RESPONSES = {
    'GetCapabilities': {'capabilities': {'model': '2204A', 'analog_channels': 2}},
    'GetAcquisition': {'mode': 'average', 'average_count': 64},
    'GetRoll': {'roll': 'on', 'rolling': 1},
    'GetHoldoff': {'holdoff_s': 0.002},
    'GetDisplay': {'display': {'persistence': 2.0}},
}


def _driver(responses=None):
    """A driver whose daemon connection is a mock."""
    scope = picoscope.PicoScope(pin='A')
    client = MagicMock()
    answers = dict(_RESPONSES, **(responses or {}))
    client.command.side_effect = lambda name, **kwargs: answers.get(name, {})
    scope._client = client
    return scope, client


def _sent(client, name):
    """The parameters of every `name` command sent, in order."""
    return [call.kwargs for call in client.command.call_args_list
            if call.args[0] == name]


class GetStateTests(unittest.TestCase):

    def test_channels_are_named_by_letter(self):
        # The daemon's JSON carries its Rust enum, which reached `lager scope
        # status` and printed as {'Alphabetic': 'A'}.
        state = {
            'channels': [{'channel': {'Alphabetic': 'A'}, 'enabled': True},
                         {'channel': {'Alphabetic': 'B'}, 'enabled': False}],
            'trigger': {'source': {'Alphabetic': 'B'}, 'level': 0.5},
            'display': {'math': {'expr': 'A-B'}},
        }
        scope, _ = _driver({'GetState': {'state': state}})
        plain = scope.get_state()
        self.assertEqual([c['channel'] for c in plain['channels']], ['A', 'B'])
        self.assertEqual(plain['trigger']['source'], 'B')
        self.assertEqual(plain['trigger']['level'], 0.5)
        self.assertEqual(plain['display'], {'math': {'expr': 'A-B'}})

    def test_no_state_is_an_empty_dict(self):
        scope, _ = _driver({'GetState': {}})
        self.assertEqual(scope.get_state(), {})


class AcquisitionTests(unittest.TestCase):

    def test_a_bench_scope_name_is_sent_as_the_daemon_token(self):
        scope, client = _driver()
        scope.set_acquisition('avg', 64)
        self.assertEqual(_sent(client, 'SetAcquisition'),
                         [{'mode': 'average', 'average_count': 64}])

    def test_no_count_leaves_the_count_to_the_daemon(self):
        scope, client = _driver()
        scope.set_acquisition('sample')
        self.assertEqual(_sent(client, 'SetAcquisition'),
                         [{'mode': 'normal', 'average_count': None}])

    def test_setting_reads_back_what_the_daemon_holds(self):
        scope, _ = _driver()
        self.assertEqual(scope.set_acquisition('average', 64),
                         {'mode': 'average', 'average_count': 64})

    def test_an_unknown_mode_is_refused_before_the_wire(self):
        scope, client = _driver()
        with self.assertRaises(ValueError):
            scope.set_acquisition('smooth')
        self.assertEqual(_sent(client, 'SetAcquisition'), [])


class HoldoffAndRollTests(unittest.TestCase):

    def test_holdoff_is_sent_in_seconds(self):
        scope, client = _driver()
        self.assertEqual(scope.set_trigger_holdoff('1e-3'), {'holdoff': 0.001})
        self.assertEqual(_sent(client, 'SetHoldoff'), [{'holdoff_s': 0.001}])

    def test_holdoff_reads_back_as_a_float(self):
        scope, _ = _driver()
        self.assertEqual(scope.get_trigger_holdoff(), 0.002)

    def test_roll_takes_yes_and_no(self):
        scope, client = _driver()
        scope.set_roll('yes')
        scope.set_roll('no')
        self.assertEqual(_sent(client, 'SetRoll'), [{'roll': 'on'}, {'roll': 'off'}])

    def test_roll_reports_whether_it_is_rolling_now(self):
        scope, _ = _driver()
        self.assertEqual(scope.get_roll(), {'roll': 'on', 'rolling': True})

    def test_an_unknown_roll_mode_is_refused(self):
        scope, _ = _driver()
        with self.assertRaises(ValueError):
            scope.set_roll('sometimes')


class DisplayTests(unittest.TestCase):

    def _patch(self, **settings):
        scope, client = _driver()
        scope.set_display(**settings)
        (sent,) = _sent(client, 'SetDisplay')
        return sent['display']

    def test_only_the_settings_given_are_sent(self):
        self.assertEqual(self._patch(persistence=2), {'persistence': 2.0})

    def test_persistence_takes_infinite_and_off(self):
        self.assertEqual(self._patch(persistence='infinite'), {'persistence': 'infinite'})
        self.assertEqual(self._patch(persistence='off'), {'persistence': None})
        self.assertEqual(self._patch(persistence=0), {'persistence': None})

    def test_persistence_beyond_a_minute_is_refused(self):
        scope, _ = _driver()
        for value in (61, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    scope.set_display(persistence=value)

    def test_zoom_carries_its_centre(self):
        self.assertEqual(self._patch(zoom={'factor': 8, 'center': 1e-3}),
                         {'zoom': {'factor': 8.0, 'center': 1e-3}})

    def test_zoom_of_one_is_no_zoom(self):
        self.assertEqual(self._patch(zoom=1), {'zoom': None})

    def test_zoom_outside_its_range_is_refused(self):
        scope, _ = _driver()
        for factor in (0.5, 1001):
            with self.subTest(factor=factor):
                with self.assertRaises(ValueError):
                    scope.set_display(zoom=factor)

    def test_xy_is_on_or_cleared(self):
        self.assertEqual(self._patch(xy='on'), {'xy': True})
        self.assertEqual(self._patch(xy='off'), {'xy': None})

    def test_math_is_normalized(self):
        self.assertEqual(self._patch(math='a - b'), {'math': {'expr': 'A-B'}})
        self.assertEqual(self._patch(math='B*A'), {'math': {'expr': 'B*A'}})

    def test_math_needs_two_different_channels_and_an_operator(self):
        scope, _ = _driver()
        for expr in ('a-a', 'a/b', 'a', 'ab-c'):
            with self.subTest(expr=expr):
                with self.assertRaises(ValueError):
                    scope.set_display(math=expr)

    def test_math_on_a_channel_the_unit_lacks_is_refused(self):
        scope, _ = _driver()
        with self.assertRaises(picoscope.UnsupportedScopeFeature):
            scope.set_display(math='a-c')

    def test_fft_defaults_to_a_hann_window(self):
        self.assertEqual(self._patch(fft='b'), {'fft': {'channel': 'B', 'window': 'hann'}})

    def test_an_unknown_fft_window_is_refused(self):
        scope, _ = _driver()
        with self.assertRaises(ValueError):
            scope.set_display(fft={'channel': 'A', 'window': 'kaiser'})

    def test_an_unknown_setting_is_refused_by_name(self):
        scope, _ = _driver()
        with self.assertRaisesRegex(ValueError, 'brightness'):
            scope.set_display(brightness=5)

    def test_no_setting_at_all_is_refused(self):
        scope, _ = _driver()
        with self.assertRaises(ValueError):
            scope.set_display()


class CursorPublishingTests(unittest.TestCase):
    """Cursors placed from the terminal appear on a page already open."""

    def test_placing_cursors_hands_them_to_the_daemon(self):
        scope, client = _driver()
        scope.set_cursors(time=[1e-3, 2e-3])
        (sent,) = _sent(client, 'SetDisplay')
        self.assertEqual(sent['display']['cursors']['time'], [1e-3, 2e-3])

    def test_clearing_them_removes_them_from_the_display(self):
        scope, client = _driver()
        scope.set_cursors(time=[1e-3, 2e-3])
        scope.clear_cursors()
        self.assertEqual(_sent(client, 'SetDisplay')[-1], {'display': {'cursors': None}})

    def test_an_unreachable_daemon_does_not_fail_the_command(self):
        scope, client = _driver()

        def refuse(name, **kwargs):
            if name == 'SetDisplay':
                raise daemon_client.ScopeDaemonError('no daemon')
            return {}
        client.command.side_effect = refuse
        cursors = scope.set_cursors(volts=[0.5, -0.5])
        self.assertEqual(cursors['volts'], [0.5, -0.5])


class _Frame:
    """Enough of an lscp.CaptureFrame for spectrum_peaks."""

    def __init__(self, volts, interval_ns, envelope=False):
        self._volts = np.asarray(volts, dtype=float)
        self.sample_interval_ns = interval_ns
        self.is_envelope = envelope

    def channel_index(self, label):
        return 0 if label == 'A' else None

    def volts(self, label):
        return self._volts


def _tone(frequency, amplitude, count=4096, rate=1e6):
    t = np.arange(count) / rate
    return amplitude * np.sin(2 * np.pi * frequency * t)


class SpectrumTests(unittest.TestCase):

    def test_a_tone_is_found_at_its_frequency_and_rms_amplitude(self):
        # 1 MS/s over 4096 samples is 244 Hz bins; 10 kHz sits between two,
        # which the parabolic fit has to recover.
        frame = _Frame(_tone(10e3, 1.0), interval_ns=1000.0)
        result = picoscope.spectrum_peaks(frame, 'A', window='flattop', peaks=1)
        (peak,) = result['peaks']
        self.assertAlmostEqual(peak['frequency_hz'], 10e3, delta=result['resolution_hz'] / 4)
        # The flat-top window is flat to a fraction of a percent across a bin.
        self.assertAlmostEqual(peak['vrms'], 1 / math.sqrt(2), delta=0.01)
        self.assertAlmostEqual(peak['dbv'], -3.01, delta=0.1)

    def test_peaks_come_strongest_first(self):
        volts = _tone(10e3, 1.0) + _tone(30e3, 0.25)
        result = picoscope.spectrum_peaks(_Frame(volts, 1000.0), 'A', window='hann', peaks=2)
        frequencies = [p['frequency_hz'] for p in result['peaks']]
        self.assertAlmostEqual(frequencies[0], 10e3, delta=result['resolution_hz'])
        self.assertAlmostEqual(frequencies[1], 30e3, delta=result['resolution_hz'])

    def test_dc_is_not_a_peak(self):
        volts = 5.0 + _tone(10e3, 0.1)
        (peak,) = picoscope.spectrum_peaks(_Frame(volts, 1000.0), 'A', peaks=1)['peaks']
        self.assertAlmostEqual(peak['frequency_hz'], 10e3, delta=250)

    def test_a_rolling_screen_is_read_as_pair_midpoints(self):
        # Each (min, max) pair spans two sample intervals, so a tone in the
        # midpoints is at the frequency of the pairs, not of the samples.
        midpoints = _tone(1e3, 1.0, count=2048, rate=20e3)
        pairs = np.repeat(midpoints, 2) + np.tile([-0.01, 0.01], 2048)
        frame = _Frame(pairs, interval_ns=25e3, envelope=True)
        result = picoscope.spectrum_peaks(frame, 'A', peaks=1)
        self.assertEqual(result['samples'], 2048)
        self.assertAlmostEqual(result['sample_rate_hz'], 20e3)
        self.assertAlmostEqual(result['peaks'][0]['frequency_hz'], 1e3, delta=result['resolution_hz'])

    def test_samples_not_yet_captured_are_left_out(self):
        volts = _tone(10e3, 1.0)
        volts[:100] = np.nan
        result = picoscope.spectrum_peaks(_Frame(volts, 1000.0), 'A', peaks=1)
        self.assertEqual(result['samples'], 4096 - 100)

    def test_a_disabled_channel_is_refused_by_name(self):
        with self.assertRaisesRegex(picoscope.UnsupportedScopeFeature, 'channel B'):
            picoscope.spectrum_peaks(_Frame(_tone(1e3, 1.0), 1000.0), 'B')

    def test_too_few_samples_are_refused(self):
        with self.assertRaises(picoscope.UnsupportedScopeFeature):
            picoscope.spectrum_peaks(_Frame(np.zeros(8), 1000.0), 'A')

    def test_an_unknown_window_is_refused(self):
        with self.assertRaises(ValueError):
            picoscope.spectrum_peaks(_Frame(_tone(1e3, 1.0), 1000.0), 'A', window='kaiser')


if __name__ == '__main__':
    unittest.main()
