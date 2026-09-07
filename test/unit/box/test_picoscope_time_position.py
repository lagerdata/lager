# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the PicoScope driver's horizontal position
(box/lager/measurement/scope/picoscope.py).

The daemon has carried a ``SetTimeOffset`` command all along, and it stored
the number faithfully -- but nothing ever read it back out, so setting a time
offset moved nothing. ``lager scope`` and ``trace_settings.set_time_offset()``
both reached it and both silently did nothing.

What actually moves the window is where the trigger sits inside the block, so
the offset is translated into that split. Two things then have to hold, and
they are what these tests pin:

* the translation, including its limits -- a block is all the scope holds, so
  the travel is one window each way and asking for more has to clamp rather
  than send a nonsense split;
* that arming a capture keeps the position. Every StartAcquisition carries the
  split and the daemon adopts whatever it is handed, so a hardcoded 50% on the
  Run path would recentre a shifted window -- the control would appear to
  work, then undo itself on the next capture.
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
             'serial', 'serial.tools', 'serial.tools.list_ports'):
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.measurement.scope import picoscope  # noqa: E402


# 8000 samples at 100 MS/s: a window of exactly 80 us, so half of it is 40 us
# and the arithmetic below can be read without a calculator.
WINDOW_SECONDS = 80e-6
_RESPONSES = {
    'GetSampleRate': {'sample_rate': 1e8},
    'GetMemoryDepth': {'memory_depth': 8000},
    'GetTimeOffset': {'time_offset': 0.0},
}


def _driver(**responses):
    """A driver whose daemon connection is a mock, with overridable replies."""
    scope = picoscope.PicoScope(pin='A')
    answers = dict(_RESPONSES, **responses)
    client = MagicMock()
    client.command.side_effect = lambda name, **kwargs: answers.get(name, {})
    scope._client = client
    return scope, client


def _commands(client):
    return [call.args[0] for call in client.command.call_args_list]


def _params_for(client, name):
    for call in client.command.call_args_list:
        if call.args[0] == name:
            return call.kwargs
    raise AssertionError(f'{name} was never sent (sent: {_commands(client)})')


def _armed_at(client):
    """The pre/post-trigger split the driver armed with."""
    return _params_for(client, 'StartAcquisition')['trigger_position_percent']


class TheOffsetBecomesATriggerPositionTests(unittest.TestCase):
    """An offset in seconds is a fraction of the window in the end."""

    def test_no_offset_leaves_the_trigger_in_the_middle(self):
        scope, client = _driver()
        scope.set_timebase_offset(0)
        self.assertEqual(_armed_at(client), 50.0)

    def test_looking_forward_half_a_window_puts_the_trigger_at_the_start(self):
        """All post-trigger: the screen is entirely what came after."""
        scope, client = _driver()
        scope.set_timebase_offset(WINDOW_SECONDS / 2)
        self.assertEqual(_armed_at(client), 0.0)

    def test_looking_back_half_a_window_puts_the_trigger_at_the_end(self):
        scope, client = _driver()
        scope.set_timebase_offset(-WINDOW_SECONDS / 2)
        self.assertEqual(_armed_at(client), 100.0)

    def test_a_quarter_window_is_a_quarter_of_the_way_across(self):
        scope, client = _driver()
        scope.set_timebase_offset(WINDOW_SECONDS / 4)
        self.assertEqual(_armed_at(client), 25.0)

    def test_asking_for_more_than_the_block_holds_stops_at_the_edge(self):
        """A split outside 0-100 is not a thing the scope can be asked for."""
        scope, client = _driver()
        scope.set_timebase_offset(WINDOW_SECONDS * 10)
        self.assertEqual(_armed_at(client), 0.0)

    def test_the_same_is_true_looking_backwards(self):
        scope, client = _driver()
        scope.set_timebase_offset(-WINDOW_SECONDS * 10)
        self.assertEqual(_armed_at(client), 100.0)

    def test_the_offset_is_stored_as_well_as_applied(self):
        """Stored so it reads back, applied so it does something.

        The daemon's own copy is write-only, which is why both are needed.
        """
        scope, client = _driver()
        scope.set_timebase_offset(1e-5)
        self.assertEqual(_params_for(client, 'SetTimeOffset')['time_offset'], 1e-5)
        sent = _commands(client)
        self.assertLess(sent.index('SetTimeOffset'), sent.index('StartAcquisition'),
                        'store it before arming with it')

    def test_a_scope_that_cannot_report_its_window_is_left_centred(self):
        """Rather than dividing by a zero-length window."""
        scope, client = _driver(GetSampleRate={'sample_rate': 0.0})
        scope.set_timebase_offset(1e-5)
        self.assertEqual(_armed_at(client), 50.0)


class ArmingKeepsThePositionTests(unittest.TestCase):
    """The bug this would otherwise have: a control that undoes itself."""

    def test_run_keeps_a_shifted_window(self):
        scope, client = _driver(GetTimeOffset={'time_offset': WINDOW_SECONDS / 2})
        scope.run()
        self.assertEqual(_armed_at(client), 0.0,
                         'Run recentred a window the user had shifted')

    def test_single_keeps_a_shifted_window(self):
        scope, client = _driver(GetTimeOffset={'time_offset': -WINDOW_SECONDS / 4})
        scope.single()
        self.assertEqual(_armed_at(client), 75.0)

    def test_run_without_an_offset_is_centred_as_before(self):
        scope, client = _driver()
        scope.run()
        self.assertEqual(_armed_at(client), 50.0)

    def test_an_unshifted_window_costs_no_extra_round_trips(self):
        """The common case must not pay for the feature.

        With no offset set there is nothing to translate, so the window's
        length is not worth asking for -- it takes two commands to learn.
        """
        scope, client = _driver()
        scope.run()
        sent = _commands(client)
        self.assertNotIn('GetSampleRate', sent)
        self.assertNotIn('GetMemoryDepth', sent)

    def test_streaming_keeps_the_position_too(self):
        scope, client = _driver(GetTimeOffset={'time_offset': WINDOW_SECONDS / 2},
                                GetCapabilities={'capabilities': {
                                    'model': '2204A', 'analog_channels': 2}})
        scope.stream_start(channel='A')
        self.assertEqual(_armed_at(client), 0.0)


class TheOffsetReadsBackTests(unittest.TestCase):

    def test_the_stored_offset_is_returned(self):
        scope, _ = _driver(GetTimeOffset={'time_offset': 2.5e-5})
        self.assertEqual(scope.get_timebase_offset(), 2.5e-5)

    def test_a_daemon_that_omits_the_field_reads_as_no_offset(self):
        """Every arm reads this, so a missing field must not take Run down."""
        scope, _ = _driver(GetTimeOffset={})
        self.assertEqual(scope.get_timebase_offset(), 0.0)


if __name__ == '__main__':
    unittest.main()
