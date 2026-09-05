# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the PicoScope driver's streaming API
(box/lager/measurement/scope/picoscope.py).

``stream_start`` and ``stream_capture`` were documented in the Python
reference with a specific set of keyword arguments before either existed, so
the signature is the contract: a script copied out of the docs has to run.
These tests pin the documented arguments and the CSV layout, which matches
what `lager scope stream capture` writes so either route produces the same
file.
"""

import csv
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

from lager.measurement.scope import picoscope  # noqa: E402


class _FakeChannel:
    def __init__(self, label):
        self.channel = label


class _FakeFrame:
    """Enough of an lscp.CaptureFrame for the CSV writer."""

    def __init__(self, labels=('A',), samples=4, interval_ns=1000.0):
        self.channels = [_FakeChannel(label) for label in labels]
        self.samples_per_channel = samples
        self.sample_interval_ns = interval_ns
        self._per_channel = samples

    def volts(self, index):
        # Distinct per channel, so a mixed-up column shows up as wrong data.
        return [float(index * 100 + i) for i in range(self._per_channel)]


# stream_start reads these back to report what it armed, so a mock that
# answers {} makes float(None) the failure instead of whatever is being tested.
_RESPONSES = {
    'GetSampleRate': {'sample_rate': 1e8},
    'GetMemoryDepth': {'memory_depth': 8000},
    'GetCapabilities': {'capabilities': {'model': '2204A', 'analog_channels': 2}},
}


def _driver(frames=None):
    """A driver whose daemon connection is a mock."""
    scope = picoscope.PicoScope(pin='A')
    client = MagicMock()
    client.command.side_effect = lambda name, **kwargs: _RESPONSES.get(name, {})
    if frames is not None:
        client.capture.side_effect = list(frames)
    scope._client = client
    return scope, client


def _commands(client):
    """The command names sent, in order."""
    return [call.args[0] for call in client.command.call_args_list]


def _params_for(client, name):
    for call in client.command.call_args_list:
        if call.args[0] == name:
            return call.kwargs
    raise AssertionError(f'{name} was never sent (sent: {_commands(client)})')


class StreamStartSignatureTests(unittest.TestCase):
    """Exactly the arguments the Python reference documents."""

    def test_the_documented_call_from_the_docs_works(self):
        scope, client = _driver()
        info = scope.stream_start(
            channel='A',
            volts_per_div=1.0,
            time_per_div=0.001,
            trigger_level=0.5,
            trigger_slope='rising',
            capture_mode='auto',
            coupling='dc',
        )
        self.assertEqual(info['channels'], ['A'])

    def test_each_setting_is_pushed_to_the_daemon(self):
        scope, client = _driver()
        scope.stream_start(channel='A', volts_per_div=2.0, time_per_div=0.005,
                           trigger_level=0.25, trigger_slope='falling',
                           coupling='ac')
        self.assertEqual(_params_for(client, 'SetVoltsPerDiv')['volts_per_div'], 2.0)
        self.assertEqual(_params_for(client, 'SetTimePerDiv')['time_per_div'], 0.005)
        self.assertEqual(_params_for(client, 'SetTriggerLevel')['trigger_level'], 0.25)
        self.assertEqual(_params_for(client, 'SetTriggerSlope')['trigger_slope'], 'falling')
        self.assertEqual(_params_for(client, 'SetCoupling')['coupling'], 'AC')

    def test_omitted_settings_are_left_alone(self):
        # Arming with a new level must not reset a coupling set earlier.
        scope, client = _driver()
        scope.stream_start(channel='A', trigger_level=0.1)
        sent = _commands(client)
        self.assertNotIn('SetCoupling', sent)
        self.assertNotIn('SetVoltsPerDiv', sent)
        self.assertNotIn('SetTimePerDiv', sent)
        self.assertNotIn('SetTriggerSlope', sent)

    def test_a_trigger_level_points_the_source_at_the_channel_first(self):
        # A level is in volts on whichever channel triggers, so setting one
        # without the other applies it to the wrong input.
        scope, client = _driver()
        scope.stream_start(channel='B', trigger_level=0.4)
        sent = _commands(client)
        self.assertLess(sent.index('SetTriggerSource'), sent.index('SetTriggerLevel'))
        self.assertEqual(_params_for(client, 'SetTriggerSource')['trigger_source'],
                         {'Alphabetic': 'B'})

    def test_capture_mode_defaults_to_auto(self):
        scope, client = _driver()
        scope.stream_start(channel='A')
        self.assertEqual(_params_for(client, 'SetCaptureMode')['capture_mode'], 'auto')

    def test_capture_mode_is_lowercased_for_the_daemon(self):
        # The daemon answers "unknown variant" for the wrong case.
        scope, client = _driver()
        scope.stream_start(channel='A', capture_mode='Single')
        self.assertEqual(_params_for(client, 'SetCaptureMode')['capture_mode'], 'single')

    def test_several_channels_can_be_armed_together(self):
        scope, client = _driver()
        info = scope.stream_start(channels=['A', 'B'])
        self.assertEqual(info['channels'], ['A', 'B'])

    def test_channel_and_channels_together_enable_the_union_once(self):
        scope, client = _driver()
        info = scope.stream_start(channel='A', channels=['A', 'B'])
        self.assertEqual(info['channels'], ['A', 'B'])

    def test_a_bad_capture_mode_is_rejected_before_the_wire(self):
        scope, client = _driver()
        with self.assertRaises(ValueError):
            scope.stream_start(channel='A', capture_mode='sometimes')


class StreamCaptureTests(unittest.TestCase):
    """``stream_capture(output, duration, samples)``, as documented."""

    def setUp(self):
        self.path = os.path.join(
            os.path.dirname(__file__), '_stream_capture_test.csv')
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def _read(self):
        with open(self.path, newline='') as handle:
            return list(csv.reader(handle))

    def test_it_writes_the_same_columns_the_cli_writes(self):
        scope, _ = _driver(frames=[_FakeFrame(samples=4)])
        scope.stream_capture(output=self.path, duration=0.05, samples=4)
        rows = self._read()
        self.assertEqual(rows[0],
                         ['capture', 'channel', 'sample_index', 'time_ns', 'voltage'])

    def test_the_time_column_follows_the_sample_interval(self):
        scope, _ = _driver(frames=[_FakeFrame(samples=3, interval_ns=1280.0)])
        scope.stream_capture(output=self.path, duration=0.05, samples=3)
        rows = self._read()[1:]
        self.assertEqual([row[3] for row in rows], ['0.0', '1280.0', '2560.0'])

    def test_samples_caps_the_rows_per_channel(self):
        scope, _ = _driver(frames=[_FakeFrame(samples=1000)])
        result = scope.stream_capture(output=self.path, duration=0.05, samples=10)
        self.assertEqual(result['samples_per_channel'], 10)
        self.assertEqual(len(self._read()) - 1, 10)

    def test_every_channel_gets_its_own_rows(self):
        scope, _ = _driver(frames=[_FakeFrame(labels=('A', 'B'), samples=2)])
        scope.stream_capture(output=self.path, duration=0.05, samples=2)
        rows = self._read()[1:]
        self.assertEqual([row[1] for row in rows], ['A', 'A', 'B', 'B'])

    def test_it_returns_a_summary_rather_than_the_samples(self):
        scope, _ = _driver(frames=[_FakeFrame(samples=4)])
        result = scope.stream_capture(output=self.path, duration=0.05, samples=4)
        self.assertEqual(result['captures'], 1)
        self.assertEqual(result['output'], self.path)

    def test_no_output_path_means_no_file_and_no_row_building(self):
        scope, _ = _driver(frames=[_FakeFrame(samples=1000)])
        result = scope.stream_capture(duration=0.05, samples=10)
        self.assertEqual(result['rows'], 0)
        self.assertIsNone(result['output'])
        self.assertEqual(result['samples_per_channel'], 10)

    def test_a_capture_that_never_arrives_keeps_what_was_collected(self):
        from lager.measurement.scope import daemon_client
        scope, client = _driver()
        client.capture.side_effect = [
            _FakeFrame(samples=2),
            daemon_client.ScopeDaemonError('timed out'),
        ]
        result = scope.stream_capture(output=self.path, duration=5.0)
        self.assertEqual(result['captures'], 1)
        self.assertEqual(len(self._read()) - 1, 2)

    def test_a_nonpositive_duration_is_rejected(self):
        scope, _ = _driver()
        for duration in (0, -1.0):
            with self.subTest(duration=duration):
                with self.assertRaises(ValueError):
                    scope.stream_capture(duration=duration)

    def test_a_nonpositive_sample_cap_is_rejected(self):
        scope, _ = _driver()
        for samples in (0, -5):
            with self.subTest(samples=samples):
                with self.assertRaises(ValueError):
                    scope.stream_capture(duration=1.0, samples=samples)


class StreamFramesTests(unittest.TestCase):
    """The zero-copy path, for samples going into numpy rather than a file."""

    def test_it_yields_the_frames_it_is_asked_for(self):
        frames = [_FakeFrame(), _FakeFrame(), _FakeFrame()]
        scope, _ = _driver(frames=frames)
        self.assertEqual(len(list(scope.stream_frames(count=3))), 3)

    def test_it_yields_one_by_default(self):
        scope, _ = _driver(frames=[_FakeFrame()])
        self.assertEqual(len(list(scope.stream_frames())), 1)


if __name__ == '__main__':
    unittest.main()
