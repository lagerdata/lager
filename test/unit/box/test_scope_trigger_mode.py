# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for who is allowed to change a PicoScope's trigger mode.

Three things wrote the capture mode and nothing read it back, which is how a
scope came to be in a mode nobody had chosen:

* ``run()`` forced it to auto, so selecting Normal and pressing Run left the
  scope free-running -- the trace kept sliding about as though the trigger were
  being ignored, because it was;
* ``single()`` sets it to single, and the daemon returns a completed
  single-shot to Normal, so the mode moves without anyone asking;
* the web UI sent all four trigger settings on every change, from a panel that
  had never read any of them, so nudging the level re-asserted a mode taken
  from the HTML rather than from the instrument.

The tests below pin the mode as something only an explicit request moves, with
one exception: running continuously contradicts a single-shot, so Run promotes
that one.
"""

import json
import os
import pathlib
import subprocess
import sys
import types
import unittest
from unittest.mock import MagicMock

import pytest


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

_REPO = pathlib.Path(__file__).resolve().parents[3]
_BOX_ROOT = _REPO / 'box'
if str(_BOX_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOX_ROOT))

from lager.measurement.scope import picoscope  # noqa: E402

SCOPE_JS = _BOX_ROOT / 'lager' / 'static' / 'scope' / 'scope.js'

_RESPONSES = {
    'GetSampleRate': {'sample_rate': 1e8},
    'GetMemoryDepth': {'memory_depth': 8000},
    'GetTimeOffset': {'time_offset': 0.0},
}


def _driver(mode='normal', **responses):
    """A driver whose daemon connection is a mock, reporting `mode`."""
    scope = picoscope.PicoScope(pin='A')
    answers = dict(_RESPONSES)
    answers['GetCaptureMode'] = {'capture_mode': mode}
    answers.update(responses)
    client = MagicMock()
    client.command.side_effect = lambda name, **kwargs: answers.get(name, {})
    scope._client = client
    return scope, client


def _sent(client, name):
    """Every set of kwargs sent under `name`, in order."""
    return [call.kwargs for call in client.command.call_args_list
            if call.args[0] == name]


class RunKeepsTheModeItWasGiven(unittest.TestCase):
    """Run starts the sweep. It does not choose how the sweep is triggered."""

    def test_normal_survives_a_run(self):
        """The reported bug: Normal selected, Run pressed, trace still moving
        about as though on Auto -- because Run had set Auto."""
        scope, client = _driver(mode='normal')
        scope.run()
        self.assertEqual(_sent(client, 'SetCaptureMode'), [],
                         'Run rewrote a mode the caller had chosen')

    def test_auto_survives_a_run(self):
        scope, client = _driver(mode='auto')
        scope.run()
        self.assertEqual(_sent(client, 'SetCaptureMode'), [])

    def test_a_run_still_arms(self):
        """Not changing the mode must not stop it starting."""
        scope, client = _driver(mode='normal')
        scope.run()
        self.assertTrue(_sent(client, 'StartAcquisition'),
                        'Run did not arm the scope')

    def test_a_single_shot_is_promoted_because_it_would_stop_at_one(self):
        """The one mode Run has to move: a single-shot disarms after one
        capture, so running in it would take one frame and look inert."""
        scope, client = _driver(mode='single')
        scope.run()
        self.assertEqual(_sent(client, 'SetCaptureMode'),
                         [{'capture_mode': 'auto'}])

    def test_single_still_sets_single(self):
        scope, client = _driver(mode='auto')
        scope.single()
        self.assertEqual(_sent(client, 'SetCaptureMode'),
                         [{'capture_mode': 'single'}])
        self.assertTrue(_sent(client, 'StartAcquisition'))


class ChoosingSingleShotArmsIt(unittest.TestCase):
    """Setting the mode alone left the scope in single-shot but unarmed, so
    nothing happened until a capture was started -- and starting one promotes
    single-shot to auto, it being the mode that cannot run continuously. So
    choosing single from the trigger menu was either inert or self-cancelling,
    while the Single button, which arms, worked. Both now arm."""

    def _apply(self, mode):
        dev = MagicMock()
        from lager.http_handlers import net_command
        net_command._scope_trigger_edge(dev, {"mode": mode})
        return dev

    def test_single_arms(self):
        dev = self._apply("single")
        dev.single.assert_called_once_with()
        dev.set_capture_mode.assert_not_called()

    def test_single_is_matched_whatever_its_case(self):
        dev = self._apply("SINGLE")
        dev.single.assert_called_once_with()

    def test_the_other_modes_only_set_the_mode(self):
        """Auto and normal are how a running sweep triggers, so selecting one
        must not arm anything."""
        for mode in ("auto", "normal"):
            dev = self._apply(mode)
            dev.set_capture_mode.assert_called_once_with(mode)
            dev.single.assert_not_called()


class TheTriggerCanBeReadBack(unittest.TestCase):
    """`trigger_edge` could set all of this and nothing could read it."""

    def _handler(self):
        sys.path.insert(0, str(_BOX_ROOT / 'lager'))
        from lager.http_handlers import net_command
        return net_command

    def test_the_four_getters_are_routed(self):
        source = (_BOX_ROOT / 'lager' / 'http_handlers'
                  / 'net_command.py').read_text()
        for action in ('get_capture_mode', 'get_trigger_source',
                       'get_trigger_slope', 'get_trigger_level'):
            self.assertIn('action == "%s"' % action, source,
                          '%s cannot be read back' % action)

    def test_none_of_them_are_per_channel(self):
        """One trigger per scope, so these belong to the instrument net and
        must not be gated to a channel."""
        net_command = self._handler()
        for action in ('get_capture_mode', 'get_trigger_source',
                       'get_trigger_slope', 'get_trigger_level'):
            self.assertNotIn(action, net_command._PER_CHANNEL_SCOPE_ACTIONS)


def _run_js(body):
    """Run `body` against scope.js under node, returning its parsed stdout."""
    script = """
    (async () => {
      const { ScopeApp, timebaseChoices } = await import(%s);
      %s
    })().catch((e) => { console.error(e); process.exit(1); });
    """ % (json.dumps(SCOPE_JS.as_uri()), body)
    out = subprocess.run(
        ['node', '--input-type=module', '-e', script],
        capture_output=True, text=True, cwd=str(_REPO),
    )
    if out.returncode != 0:
        raise AssertionError('node failed: %s' % out.stderr)
    return json.loads(out.stdout) if out.stdout.strip() else None


@pytest.mark.skipif(not SCOPE_JS.exists(), reason='scope.js missing')
class TestEachTriggerControlSendsOnlyItself:
    """Sending all four fields on every change is what let a level nudge put a
    scope back into Auto, and it took the values from controls that had never
    been read back from the instrument."""

    def test_no_handler_gathers_all_four(self):
        js = SCOPE_JS.read_text()
        wiring = js.split("const sendTrigger")[1].split(
            "el('trigger-markers')")[0]
        # Each listener names one setting. A handler mentioning several would
        # be re-asserting settings the user did not touch.
        for field, others in (
            ('slope', ('mode:', 'source:', 'level')),
            ('mode', ('slope:', 'source:', 'level')),
        ):
            block = wiring.split("el('trigger-%s')" % field)[1]
            block = block.split('});')[0]
            for other in others:
                assert other not in block, (
                    'the %s control also sends %s' % (field, other))

    def test_the_panel_is_read_back_from_the_instrument(self):
        js = SCOPE_JS.read_text()
        assert 'syncTriggerState' in js
        reader = js.split('async syncTriggerState()')[1].split('\n  }')[0]
        for action in ('get_capture_mode', 'get_trigger_source',
                       'get_trigger_slope', 'get_trigger_level'):
            assert action in reader, '%s is never read' % action

    def test_the_readback_runs_on_connect_and_after_run_and_single(self):
        """Run and Single both move the mode, so the panel has to re-read or it
        goes stale the moment either is pressed."""
        js = SCOPE_JS.read_text()
        for anchor in ("el('btn-start')", "el('btn-single')"):
            block = js.split(anchor)[1].split('});')[0]
            assert 'syncTriggerState' in block, (
                '%s leaves the trigger panel stale' % anchor)

    def test_an_instrument_reporting_something_unoffered_is_left_alone(self):
        """A scope naming a source this build has no option for should not
        blank the control."""
        shown = _run_js("""
        const app = Object.create(ScopeApp.prototype);
        const controls = {
          'trigger-mode': { tagName: 'SELECT', value: 'auto',
                            options: [{ value: 'auto' }, { value: 'normal' }] },
          'trigger-source': { tagName: 'SELECT', value: 'A',
                              options: [{ value: 'A' }, { value: 'B' }] },
          'trigger-slope': { tagName: 'SELECT', value: 'rising',
                             options: [{ value: 'rising' }] },
          'trigger-level': { tagName: 'INPUT', value: '0' },
        };
        global.document = { getElementById: (id) => controls[id] || null };
        app.net = 'pico1';
        const answers = {
          get_capture_mode: 'normal',
          get_trigger_source: 'EXT',
          get_trigger_slope: 'rising',
          get_trigger_level: 1.25,
        };
        app.send = async (action) => ({ value: answers[action] });
        await app.syncTriggerState();
        process.stdout.write(JSON.stringify({
          mode: controls['trigger-mode'].value,
          source: controls['trigger-source'].value,
          level: controls['trigger-level'].value,
        }));
        """)
        assert shown['mode'] == 'normal', 'the mode was not adopted'
        assert shown['level'] == '1.25', 'the level was not adopted'
        assert shown['source'] == 'A', 'an unoffered source blanked the control'


@pytest.mark.skipif(not SCOPE_JS.exists(), reason='scope.js missing')
class TestALateTimebaseReadbackIsIgnored:
    """Two changes in quick succession each set and then read back, and the
    replies need not arrive in order -- the slower one landing last is the
    dropdown reverting to the previous setting."""

    def test_a_stale_readback_does_not_move_the_control(self):
        shown = _run_js("""
        const app = Object.create(ScopeApp.prototype);
        let displayed = null;
        app.showTimebase = (s) => { displayed = s; };
        app.runCommand = async () => {};
        app.timebaseGeneration = 0;
        app.timePositionDiv = 0;

        // The first change's readback is held up until after the second has
        // been made and has already shown its own value.
        let release;
        const held = new Promise((r) => { release = r; });
        let call = 0;
        app.send = async () => {
          call += 1;
          if (call === 1) { await held; return { value: 0.001024 }; }
          return { value: 0.008192 };
        };

        const first = app.applyTimebase(0.001024);
        await new Promise((r) => setImmediate(r));
        await app.applyTimebase(0.008192);
        const afterSecond = displayed;
        release();
        await first;

        process.stdout.write(JSON.stringify({
          afterSecond, afterLateReply: displayed,
        }));
        """)
        assert shown['afterSecond'] == 0.008192
        assert shown['afterLateReply'] == 0.008192, (
            'a readback from the previous change put the old value back')
