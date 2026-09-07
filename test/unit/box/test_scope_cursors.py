# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Scope cursors: typed, not dragged.

The CLI is the first-class control surface here, so cursors have no knob and
no draggable handle. They are placed by typing -- in the terminal
``lager scope ... cursor`` or in the web UI's console -- and the box holds the
pair so that both spellings mean the same two markers and the plot draws them.

What these tests hold down:

* The arithmetic, which lives in one free function so the CLI's answer and the
  panel's are the same answer. Interpolation matters: a cursor lands where it
  was typed, not on the sample grid, and snapping to the nearest sample is
  most wrong exactly where a cursor is most useful -- on a fast edge.
* The absences. A cursor past the end of the record has no voltage under it
  and two cursors at the same instant have no frequency, so those readings are
  left out rather than reported as zero.
* That the box and the browser interpolate the same way, since the reading in
  the terminal and the label on the plot come from different implementations
  of the same idea.
* That the renderer draws only what the box says is set, because nothing in
  the page can move a cursor on its own.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

from lager.measurement.scope.picoscope import (
    PicoScope, UnsupportedScopeFeature, cursor_readings, trace_voltage_at,
)

@pytest.fixture(autouse=True)
def _forget_cursors_between_tests():
    """Cursors are shared per scope, so they outlive the driver instance.

    That is the point of them -- the CLI places a pair and a later request
    from the page finds it -- but it also means one test's cursors are on the
    next test's scope unless the store is emptied.
    """
    from lager.measurement.scope import picoscope as _picoscope
    _picoscope._CURSORS_BY_INSTRUMENT.clear()
    yield
    _picoscope._CURSORS_BY_INSTRUMENT.clear()


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCOPE_JS = REPO_ROOT / "box" / "lager" / "static" / "scope" / "scope.js"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to run the scope UI code")


class TestReadingTheTraceUnderACursor:

    def test_a_cursor_on_a_sample_reads_that_sample(self):
        times = [0.0, 1.0, 2.0]
        volts = [0.0, 5.0, 10.0]
        assert trace_voltage_at(times, volts, 1.0) == 5.0

    def test_a_cursor_between_samples_is_interpolated(self):
        """Not snapped to the nearest sample.

        On a rising edge the nearest sample can be most of the amplitude away
        from where the cursor was put, which is the case a cursor is for.
        """
        times = [0.0, 1.0]
        volts = [0.0, 10.0]
        assert trace_voltage_at(times, volts, 0.25) == pytest.approx(2.5)
        assert trace_voltage_at(times, volts, 0.75) == pytest.approx(7.5)

    def test_a_cursor_off_either_end_reads_nothing(self):
        """Rather than the nearest end, which is a different moment."""
        times = [0.0, 1.0]
        volts = [3.0, 4.0]
        assert trace_voltage_at(times, volts, -0.1) is None
        assert trace_voltage_at(times, volts, 1.1) is None

    def test_the_ends_themselves_are_in_range(self):
        times = [0.0, 1.0]
        volts = [3.0, 4.0]
        assert trace_voltage_at(times, volts, 0.0) == 3.0
        assert trace_voltage_at(times, volts, 1.0) == 4.0

    def test_a_negative_time_is_before_the_trigger_not_out_of_range(self):
        """Half a capture sits before the trigger, so times there are normal."""
        times = [-1e-3, 0.0, 1e-3]
        volts = [1.0, 2.0, 3.0]
        assert trace_voltage_at(times, volts, -5e-4) == pytest.approx(1.5)

    def test_an_empty_capture_reads_nothing(self):
        assert trace_voltage_at([], [], 0.0) is None
        assert trace_voltage_at(None, None, 0.0) is None

    def test_a_long_record_finds_the_right_pair(self):
        """The search is a bisection, so an off-by-one would land elsewhere."""
        times = [i * 1e-6 for i in range(10000)]
        volts = [float(i) for i in range(10000)]
        assert trace_voltage_at(times, volts, 4321.5e-6) == pytest.approx(4321.5)


class TestWhatAPairOfCursorsReads:

    def test_time_cursors_report_the_interval_and_its_frequency(self):
        readings = cursor_readings(time_pair=(1e-3, 2e-3))
        assert readings["delta_t"] == pytest.approx(1e-3)
        # The point of the pair: put them a cycle apart, read the frequency.
        assert readings["frequency"] == pytest.approx(1000.0)

    def test_two_cursors_at_one_instant_have_no_frequency(self):
        """Rather than a division by zero, or an infinity presented as a read."""
        readings = cursor_readings(time_pair=(1e-3, 1e-3))
        assert readings["delta_t"] == 0
        assert "frequency" not in readings

    def test_a_backwards_pair_reads_negative_rather_than_being_reordered(self):
        """Which cursor is which is the user's business, and the sign says so."""
        readings = cursor_readings(time_pair=(2e-3, 1e-3))
        assert readings["delta_t"] == pytest.approx(-1e-3)
        assert readings["frequency"] == pytest.approx(-1000.0)

    def test_time_cursors_report_the_voltage_of_the_trace_under_each(self):
        times = [0.0, 1e-3, 2e-3, 3e-3]
        volts = [0.0, 1.0, 2.0, 3.0]
        readings = cursor_readings(time_pair=(1e-3, 3e-3),
                                   times=times, volts=volts)
        assert readings["trace_v1"] == pytest.approx(1.0)
        assert readings["trace_v2"] == pytest.approx(3.0)
        assert readings["trace_delta_v"] == pytest.approx(2.0)

    def test_a_cursor_outside_the_window_reports_the_window_and_no_voltage(self):
        """So the caller can say why the voltage is missing.

        The position reads back as exactly what was typed, so without the
        window bounds nothing in the answer hints at what went wrong.
        """
        times = [0.0, 1e-3]
        volts = [0.0, 1.0]
        readings = cursor_readings(time_pair=(0.0, 5e-3),
                                   times=times, volts=volts)
        assert readings["trace_v1"] == pytest.approx(0.0)
        assert "trace_v2" not in readings
        assert "trace_delta_v" not in readings
        assert readings["window_start"] == 0.0
        assert readings["window_end"] == pytest.approx(1e-3)

    def test_voltage_cursors_report_their_difference(self):
        readings = cursor_readings(volts_pair=(0.5, -0.5))
        assert readings["delta_v"] == pytest.approx(-1.0)

    def test_voltage_cursors_need_no_capture(self):
        """Arithmetic, so `cursor volts` does not have to wait for a trigger."""
        readings = cursor_readings(volts_pair=(1.0, 3.0), times=None, volts=None)
        assert readings["delta_v"] == pytest.approx(2.0)

    def test_both_pairs_are_reported_together(self):
        readings = cursor_readings(time_pair=(0.0, 1e-3), volts_pair=(0.0, 2.0))
        assert readings["delta_t"] == pytest.approx(1e-3)
        assert readings["delta_v"] == pytest.approx(2.0)

    def test_no_cursors_read_nothing(self):
        assert cursor_readings() == {}


class TestTheBoxHoldsTheCursors:
    """Not the browser: that is what lets the CLI place what the UI draws."""

    def _scope(self):
        return PicoScope(netname="scope1", pin=1)

    def test_a_scope_starts_with_no_cursors(self):
        cursors = self._scope().get_cursors()
        assert cursors["time"] is None
        assert cursors["volts"] is None

    def test_placing_a_pair_reads_it_back(self):
        scope = self._scope()
        scope.set_cursors(time=(1e-3, 2e-3))
        assert scope.get_cursors()["time"] == [1e-3, 2e-3]

    def test_the_pairs_are_independent(self):
        """Moving the time cursors must not clear voltage cursors."""
        scope = self._scope()
        scope.set_cursors(volts=(1.0, -1.0))
        scope.set_cursors(time=(0.0, 1e-3))
        cursors = scope.get_cursors()
        assert cursors["volts"] == [1.0, -1.0]
        assert cursors["time"] == [0.0, 1e-3]

    def test_off_clears_both(self):
        scope = self._scope()
        scope.set_cursors(time=(0.0, 1e-3), volts=(1.0, -1.0))
        cursors = scope.clear_cursors()
        assert cursors["time"] is None
        assert cursors["volts"] is None

    def test_a_cursor_is_reported_against_a_channel(self):
        """The voltage readings depend on it, so whoever draws them needs it."""
        scope = self._scope()
        assert scope.get_cursors()["channel"] == "A"
        scope.set_cursors(time=(0.0, 1e-3), channel=2)
        assert scope.get_cursors()["channel"] == "B"

    def test_a_lone_cursor_is_refused(self):
        """One cursor reads nothing; the quantity wanted is the difference."""
        scope = self._scope()
        with pytest.raises(ValueError, match="pair"):
            scope.set_cursors(time=(1e-3,))
        with pytest.raises(ValueError, match="pair"):
            scope.set_cursors(volts=(1.0, 2.0, 3.0))

    def test_reading_cursors_that_are_not_set_takes_no_capture(self):
        """A capture needs a trigger, and there is nothing to read anyway."""
        scope = self._scope()

        def fail(*_a, **_k):
            raise AssertionError("took a capture with no cursors set")

        scope.capture = fail
        assert scope.measure_cursors() == {
            "cursors": {"time": None, "volts": None, "channel": "A"},
            "readings": {},
        }

    def test_voltage_cursors_alone_take_no_capture(self):
        scope = self._scope()

        def fail(*_a, **_k):
            raise AssertionError("took a capture to subtract two numbers")

        scope.capture = fail
        scope.set_cursors(volts=(1.0, 3.0))
        result = scope.measure_cursors()
        assert result["readings"]["delta_v"] == pytest.approx(2.0)

    def test_time_cursors_read_against_a_capture(self):
        scope = self._scope()
        scope.set_cursors(time=(0.0, 1e-3))
        scope.capture = lambda **_k: _FakeFrame()
        result = scope.measure_cursors()
        # The fake ramps 0 V to 2 V over 2 ms, so a cursor at 1 ms sits at 1 V.
        assert result["readings"]["trace_v1"] == pytest.approx(0.0)
        assert result["readings"]["trace_v2"] == pytest.approx(1.0, rel=1e-3)

    def test_a_cold_scope_arms_rather_than_reporting_a_dead_one(self):
        """With nothing acquiring there is no capture coming to wait for."""
        from lager.measurement.scope import daemon_client

        scope = self._scope()
        scope.set_cursors(time=(0.0, 1e-3))
        armed = []
        attempts = []

        def capture(**_k):
            attempts.append(1)
            if len(attempts) == 1:
                raise daemon_client.ScopeDaemonError("timed out")
            return _FakeFrame()

        scope.capture = capture
        scope._start_acquisition = lambda: armed.append(1)

        result = scope.measure_cursors()
        assert armed, "gave up without arming a capture"
        assert result["readings"]["delta_t"] == pytest.approx(1e-3)


class _FakeFrame:
    """A 2 ms ramp from 0 V to 2 V, triggered at the first sample.

    Carries only the channels named, since a disabled one is absent from a
    real capture rather than present and empty.
    """

    samples_per_channel = 2001
    pre_trigger_samples = 0
    sample_interval_ns = 1000.0

    def __init__(self, channels=("A",)):
        self.channels = [c.upper() for c in channels]

    def channel_index(self, label):
        upper = str(label).upper()
        return self.channels.index(upper) if upper in self.channels else None

    def time_axis(self):
        return [i * 1e-6 for i in range(self.samples_per_channel)]

    def volts(self, channel):
        if self.channel_index(channel) is None:
            raise RuntimeError("no such channel: %r" % channel)
        return [i * 1e-3 for i in range(self.samples_per_channel)]


class TestTheHandlerFormatsWhatBothCLIsPrint:

    def _invoke(self, action, params=None, device=None):
        from lager.http_handlers import net_command

        original = net_command._proxy
        net_command._proxy = lambda *a, **k: device or _HandlerScope()
        try:
            return net_command._scope("scope1", "scope", action, params or {})
        finally:
            net_command._proxy = original

    def test_a_pair_can_be_sent_as_a_list_or_as_named_ends(self):
        """The console sends two positional args; the host CLI has options.

        One action behind both grammars, so there is one place where a cursor
        is placed and one answer for where it is.
        """
        from_console = self._invoke("set_cursor", {"time": [1e-3, 2e-3]})
        from_terminal = self._invoke("set_cursor", {"time": {"1": 1e-3, "2": 2e-3}})
        assert from_console["value"] == from_terminal["value"]
        assert from_console["value"]["time"] == [1e-3, 2e-3]

    def test_the_readings_are_in_the_message_both_CLIs_print(self):
        message = self._invoke("measure_cursor")["message"]
        # A position you just typed is not the interesting half of the answer.
        assert "delta_t" in message and "frequency" in message

    def test_an_unset_cursor_says_so_rather_than_printing_an_empty_line(self):
        device = _HandlerScope()
        device.cursors = {"time": None, "volts": None, "channel": "A"}
        device.readings = {}
        body = self._invoke("measure_cursor", device=device)
        assert "No cursors" in body["message"]

    def test_a_cursor_off_the_record_is_explained(self):
        """The position reads back fine, so the missing voltage needs saying."""
        device = _HandlerScope()
        device.readings = {"t1": 0.0, "t2": 5e-3, "delta_t": 5e-3,
                           "trace_v1": 0.5,
                           "window_start": 0.0, "window_end": 1e-3}
        message = self._invoke("measure_cursor", device=device)["message"]
        assert "t2 outside the captured window" in message
        assert "0.001" in message

    def test_clearing_reports_it(self):
        assert "off" in self._invoke("clear_cursor")["message"].lower()

    def test_placing_nothing_is_not_a_silent_success(self):
        from lager.http_handlers import net_command

        with pytest.raises(net_command.UnknownAction):
            self._invoke("set_cursor", {})

    def test_half_a_pair_is_refused_rather_than_multiplied_by_none(self):
        """Reachable through the named-ends spelling, unlike a bare list."""
        with pytest.raises(ValueError, match="pair"):
            self._invoke("set_cursor", {"time": {"1": 1e-3}})
        with pytest.raises(ValueError, match="pair"):
            self._invoke("set_cursor", {"volts": [1.0]})


class _HandlerScope:
    cursors = {"time": [1e-3, 2e-3], "volts": None, "channel": "A"}
    readings = {"t1": 1e-3, "t2": 2e-3, "delta_t": 1e-3, "frequency": 1000.0}

    def set_cursors(self, time=None, volts=None, channel=None):
        self.cursors = {"time": list(time) if time else None,
                        "volts": list(volts) if volts else None,
                        "channel": "A"}
        return self.cursors

    def get_cursors(self):
        return self.cursors

    def clear_cursors(self):
        self.cursors = {"time": None, "volts": None, "channel": "A"}
        return self.cursors

    def measure_cursors(self, channel=None, timeout=None):
        return {"cursors": self.cursors, "readings": self.readings}


@needs_node
class TestTheBrowserReadsTheTraceTheSameWayTheBoxDoes:
    """Two implementations of one idea, so they are checked against each other.

    The number in the terminal comes from Python and the label on the plot
    comes from JavaScript. If they interpolate differently, the same cursor
    reads two voltages depending on where you look.
    """

    def _sample_in_js(self, cases):
        script = """
        import { sampleTraceAt } from %s;
        const frame = { preTriggerSamples: 500, sampleIntervalNs: 1000 };
        const volts = [];
        for (let i = 0; i < 2001; i += 1) volts.push(i * 1e-3);
        const out = JSON.parse(process.env.CASES)
          .map((t) => sampleTraceAt(frame, volts, t));
        process.stdout.write(JSON.stringify(out));
        """ % json.dumps(str(SCOPE_JS))
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60, check=False,
            env=dict(os.environ, CASES=json.dumps(cases)))
        if result.returncode != 0:
            pytest.fail("node failed: %s" % result.stderr.strip())
        return json.loads(result.stdout)

    def test_the_two_agree_across_the_record(self):
        # A 2 ms ramp with the trigger 500 samples in, so times run from
        # -0.5 ms to +1.5 ms and the interesting cases are on both sides of
        # the trigger, between samples, and off each end.
        cases = [-5e-4, -2.5e-4, 0.0, 1.5e-6, 3.33e-4, 1.5e-3, -6e-4, 2e-3]
        times = [(i - 500) * 1e-6 for i in range(2001)]
        volts = [i * 1e-3 for i in range(2001)]

        from_js = self._sample_in_js(cases)
        from_python = [trace_voltage_at(times, volts, t) for t in cases]

        for case, js, python in zip(cases, from_js, from_python):
            if python is None:
                assert js is None, (
                    "the box reads nothing at %g s but the plot reads %r" % (case, js))
            else:
                assert js == pytest.approx(python, rel=1e-9, abs=1e-12), (
                    "the box and the plot disagree at %g s" % case)

    def test_both_refuse_a_cursor_off_the_end(self):
        assert self._sample_in_js([-6e-4, 2e-3]) == [None, None]


@needs_node
class TestATypedCursorReachesThePlot:
    """The point of keeping cursors on the box rather than in the page.

    Nothing in the UI can move a cursor, so the plot's copy is only ever what
    the box reports. That is what makes `lager scope ... cursor` and the
    console's `cursor` the same two markers.
    """

    def _run(self, body):
        script = """
        import { ScopeApp } from %s;
        const asked = [];
        const self = {
          net: 'scope1',
          cursors: null,
          console: { write() {}, error() {} },
          requestRedraw() { this.redrew = true; },
          refreshCursors: ScopeApp.prototype.refreshCursors,
          adoptCursors: ScopeApp.prototype.adoptCursors,
          send: async (action) => {
            asked.push(action);
            // The box answers a placement with where it stored the pair, so
            // these replies are what the plot is expected to adopt.
            if (action === 'set_cursor' || action === 'get_cursor') {
              return { value: { time: [0, 1e-3], volts: null, channel: 'A' } };
            }
            if (action === 'measure_cursor') {
              return { value: {
                cursors: { time: [0, 1e-3], volts: null, channel: 'A' },
                readings: { delta_t: 1e-3, frequency: 1000 } } };
            }
            return { message: 'ok' };
          },
        };
        %s
        """ % (json.dumps(str(SCOPE_JS)), body)
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60, check=False,
            env=dict(os.environ))
        if result.returncode != 0:
            pytest.fail("node failed: %s" % result.stderr.strip())
        return json.loads(result.stdout)

    def test_placing_a_cursor_pulls_the_plot_into_step(self):
        """The plot takes the box's answer, and redraws so it appears."""
        out = self._run("""
        await ScopeApp.prototype.runCommand.call(
          self, 'set_cursor', { time: [0, 1e-3] }, 'cursor');
        process.stdout.write(JSON.stringify({
          asked, cursors: self.cursors, redrew: !!self.redrew,
        }));
        """)
        assert out["cursors"]["time"] == [0, 1e-3]
        assert out["redrew"], "the plot was not redrawn, so nothing appeared"
        # The reply is already the box's answer for where the pair ended up,
        # so following it must not cost a second round trip.
        assert out["asked"] == ["set_cursor"]

    def test_reading_the_cursors_also_keeps_the_plot_in_step(self):
        """`measure_cursor` wraps the positions next to the readings."""
        out = self._run("""
        await ScopeApp.prototype.runCommand.call(
          self, 'measure_cursor', {}, 'cursor');
        process.stdout.write(JSON.stringify({ cursors: self.cursors }));
        """)
        assert out["cursors"]["time"] == [0, 1e-3]

    def test_clearing_drops_the_plot_copy(self):
        out = self._run("""
        self.cursors = { time: [0, 1e-3], volts: null, channel: 'A' };
        await ScopeApp.prototype.runCommand.call(self, 'clear_cursor', {}, 'off');
        process.stdout.write(JSON.stringify({
          cursors: self.cursors, redrew: !!self.redrew,
        }));
        """)
        assert out["cursors"] is None
        assert out["redrew"]

    def test_an_ordinary_command_does_not_go_looking_for_cursors(self):
        """Every command would otherwise cost an extra round trip."""
        out = self._run("""
        await ScopeApp.prototype.runCommand.call(
          self, 'set_scale', { volts_per_div: 1 }, 'scale');
        process.stdout.write(JSON.stringify({ asked }));
        """)
        assert out["asked"] == ["set_scale"]

    def test_a_box_that_has_no_cursors_leaves_the_plot_clean(self):
        """An older box rejects the action; the plot must not carry a stale pair."""
        out = self._run("""
        self.send = async () => { throw new Error('unknown action'); };
        self.cursors = { time: [0, 1e-3], volts: null, channel: 'A' };
        await ScopeApp.prototype.refreshCursors.call(self, 'scope1');
        process.stdout.write(JSON.stringify({ cursors: self.cursors }));
        """)
        assert out["cursors"] is None

    def test_a_box_with_no_cursors_set_reports_none_rather_than_an_empty_pair(self):
        out = self._run("""
        self.send = async () => (
          { value: { time: null, volts: null, channel: 'A' } });
        await ScopeApp.prototype.refreshCursors.call(self, 'scope1');
        process.stdout.write(JSON.stringify({ cursors: self.cursors }));
        """)
        assert out["cursors"] is None, (
            "an unset pair must not be truthy, or drawCursors runs on nothing")


@needs_node
class TestTheMeasurementPanelKeepsPolling:
    """A browser check reported the readouts frozen, which needed ruling out.

    The panel polls on a timer, and a hidden page's timers are throttled hard
    by the browser -- to once a second, and to once a minute after a few
    minutes hidden. Under automation, where the page is rarely painted, that
    is indistinguishable from a broken timer, so the mechanism is checked here
    instead: the interval is stubbed and its callback driven directly.
    """

    def _run(self, body):
        script = """
        import { ScopeApp } from %s;
        const scheduled = [];
        let cleared = 0;
        globalThis.setInterval = (fn, delay) => {
          scheduled.push({ fn, delay });
          return scheduled.length;
        };
        globalThis.clearInterval = () => { cleared += 1; };
        const tick = async () => {
          scheduled[scheduled.length - 1].fn();
          // Let the refresh's promise settle before anything is asserted.
          await new Promise((r) => setImmediate(r));
        };
        let refreshes = 0;
        const self = {
          measureTimer: null,
          measureInFlight: false,
          refreshMeasurements: async () => { refreshes += 1; },
          startMeasurementPolling: ScopeApp.prototype.startMeasurementPolling,
          stopMeasurementPolling: ScopeApp.prototype.stopMeasurementPolling,
          pollMeasurementsOnce: ScopeApp.prototype.pollMeasurementsOnce,
        };
        // A rejection that escapes any of the unawaited refresh paths is the
        // failure under test, so it is recorded rather than allowed to kill
        // the process with a message that names no path in particular.
        const unhandled = [];
        process.on('unhandledRejection', (e) => unhandled.push(String(e)));
        %s
        """ % (json.dumps(str(SCOPE_JS)), body)
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60, check=False,
            env=dict(os.environ))
        if result.returncode != 0:
            pytest.fail("node failed: %s" % result.stderr.strip())
        return json.loads(result.stdout)

    def test_starting_reads_once_and_then_on_a_timer(self):
        """The first reading must not wait out the interval."""
        out = self._run("""
        self.startMeasurementPolling();
        const immediate = refreshes;
        // Let the immediate read settle first, or the next tick is correctly
        // skipped as an overlap and this measures the guard instead.
        await new Promise((r) => setImmediate(r));
        await tick();
        await tick();
        process.stdout.write(JSON.stringify({
          immediate, delay: scheduled[0].delay, total: refreshes,
        }));
        """)
        assert out["immediate"] == 1, "nothing was read until the first tick"
        assert out["delay"] > 0
        assert out["total"] == 3, "the timer did not keep reading"

    def test_a_tick_while_one_is_outstanding_is_skipped(self):
        """Queued captures would arrive out of order on a slow trigger."""
        out = self._run("""
        self.startMeasurementPolling();
        self.measureInFlight = true;
        await tick();
        process.stdout.write(JSON.stringify({ total: refreshes }));
        """)
        assert out["total"] == 1, "an overlapping poll was queued anyway"

    def test_a_failed_poll_does_not_stop_the_ones_after_it(self):
        """The bug this class exists to rule out: one error freezing the panel.

        `finally` passes a rejection through rather than absorbing it, so the
        reset has to be paired with a catch or a single failure escapes the
        timer as an unhandled rejection.
        """
        out = self._run("""
        self.refreshMeasurements = async () => {
          refreshes += 1;
          throw new Error('one bad capture');
        };
        self.startMeasurementPolling();
        await new Promise((r) => setImmediate(r));
        const afterFailure = self.measureInFlight;
        self.refreshMeasurements = async () => { refreshes += 1; };
        await tick();
        process.stdout.write(JSON.stringify({
          stuck: afterFailure, total: refreshes, unhandled,
        }));
        """)
        assert out["stuck"] is False, (
            "a failed poll left the in-flight flag set, so every later tick "
            "is skipped and the panel freezes for good")
        assert out["total"] == 2, "polling did not resume after a failure"
        assert out["unhandled"] == [], (
            "a failed refresh escaped as an unhandled rejection: %s"
            % out["unhandled"])

    def test_returning_to_a_backgrounded_tab_reads_straight_away(self):
        """A hidden page's timers are throttled to as slow as once a minute.

        Without this the readouts on screen when the tab comes back describe a
        capture from up to a minute ago, which a bench scope would never do.
        """
        source = SCOPE_JS.read_text()
        assert "visibilitychange" in source, (
            "nothing refreshes on return, so a backgrounded tab shows stale "
            "measurements until the next throttled tick")
        handler = source.split("visibilitychange", 1)[1][:400]
        assert "pollMeasurementsOnce" in handler
        # Guarded on the timer: refreshing a panel that was never started, or
        # one deliberately stopped, would take a capture nobody asked for.
        assert "measureTimer" in handler

    def test_stopping_clears_the_timer(self):
        out = self._run("""
        self.startMeasurementPolling();
        self.stopMeasurementPolling();
        process.stdout.write(JSON.stringify({
          cleared, timer: self.measureTimer,
        }));
        """)
        assert out["cleared"] >= 1
        assert out["timer"] is None

    def test_restarting_does_not_leave_two_timers_running(self):
        out = self._run("""
        self.startMeasurementPolling();
        self.startMeasurementPolling();
        process.stdout.write(JSON.stringify({ cleared }));
        """)
        assert out["cleared"] >= 1, (
            "a second Start would double the capture rate against the scope")


@needs_node
class TestThePlotDrawsOnlyTheCursorsTheBoxHas:

    def _draw(self, cursors):
        """Run drawCursors and report which lines it drew."""
        script = """
        import { ScopeApp } from %s;
        globalThis.getComputedStyle = () => ({ getPropertyValue: () => '#fff' });
        const drawn = { lines: [], labels: [] };
        const ctx = {
          save() {}, restore() {}, beginPath() {}, stroke() {}, fill() {},
          setLineDash() {}, fillRect() {},
          measureText: (t) => ({ width: t.length * 6 }),
          moveTo(x, y) { this._from = [x, y]; },
          lineTo(x, y) { drawn.lines.push([...this._from, x, y]); },
          fillText(text) { drawn.labels.push(text); },
        };
        const frame = {
          samplesPerChannel: 1000, preTriggerSamples: 500,
          sampleIntervalNs: 1000,
          channels: [{ channel: 'A' }],
          volts: () => { const v = []; for (let i = 0; i < 1000; i += 1) v.push(0.1); return v; },
        };
        const self = {
          cursors: JSON.parse(process.env.CURSORS),
          channelState: new Map([['A', { voltsPerDiv: 1, positionDiv: 0 }]]),
          drawCursorLine: ScopeApp.prototype.drawCursorLine,
          timeToX: ScopeApp.prototype.timeToX,
          traceAt: ScopeApp.prototype.traceAt,
        };
        ScopeApp.prototype.drawCursors.call(self, ctx, frame, 1000, 400);
        process.stdout.write(JSON.stringify(drawn));
        """ % json.dumps(str(SCOPE_JS))
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60, check=False,
            env=dict(os.environ, CURSORS=json.dumps(cursors)))
        if result.returncode != 0:
            pytest.fail("node failed: %s" % result.stderr.strip())
        return json.loads(result.stdout)

    def test_time_cursors_are_vertical_lines_at_their_times(self):
        drawn = self._draw({"time": [0.0, 1e-4], "volts": None, "channel": "A"})
        vertical = [ln for ln in drawn["lines"] if ln[0] == ln[2]]
        assert len(vertical) == 2
        # The trigger is 500 of 1000 samples in, so t=0 is the centre of a
        # 1000 px plot and 100 us later is 100 samples further right.
        assert [round(ln[0]) for ln in vertical] == [500, 600]

    def test_voltage_cursors_are_horizontal_lines_at_their_volts(self):
        drawn = self._draw({"time": None, "volts": [2.0, -2.0], "channel": "A"})
        horizontal = [ln for ln in drawn["lines"] if ln[1] == ln[3]]
        assert len(horizontal) == 2
        # 1 V/div over eight divisions: +2 V is two divisions above centre,
        # which on a 400 px plot is 100 px up.
        assert [round(ln[1]) for ln in horizontal] == [100, 300]

    def test_a_cursor_is_drawn_against_its_own_channel_scale(self):
        """Halve the volts/div and the same voltage sits twice as far out."""
        script_state = {"time": None, "volts": [1.0, -1.0], "channel": "A"}
        at_1v = self._draw(script_state)
        assert [round(ln[1]) for ln in at_1v["lines"] if ln[1] == ln[3]] \
            == [150, 250]

    def test_the_readout_reports_the_deltas(self):
        drawn = self._draw({"time": [0.0, 1e-4], "volts": None, "channel": "A"})
        labels = " ".join(drawn["labels"])
        assert "\u0394t" in labels
        # A pair 100 us apart is a 10 kHz spacing, which is the useful read.
        assert "10.00 kHz" in labels
        assert "100.0 \u00b5s" in labels

    def test_each_cursor_is_named_so_the_pair_can_be_told_apart(self):
        drawn = self._draw({"time": [0.0, 1e-4], "volts": [1.0, -1.0],
                            "channel": "A"})
        labels = drawn["labels"]
        for name in ("t1", "t2", "v1", "v2"):
            assert name in labels, "no %s label; two dashed lines look alike" % name

    def test_a_cursor_past_the_edge_is_pinned_rather_than_lost(self):
        """Off screen and invisible reads as unset, which it is not."""
        drawn = self._draw({"time": [0.0, 1.0], "volts": None, "channel": "A"})
        vertical = [ln for ln in drawn["lines"] if ln[0] == ln[2]]
        assert len(vertical) == 2
        # One second out on a 1 ms window: pinned to the right edge of the
        # 1000 px plot rather than drawn a thousand screens away.
        assert max(ln[0] for ln in vertical) < 1000


class TestReadingCursorsOnADisabledChannel:
    """A disabled channel is missing from the capture, not empty in it.

    Handed to the frame it reads as a channel the scope does not have, which
    points at the wrong problem: the channel exists, it is just switched off.
    The daemon already says this well for measurements, so cursors say it the
    same way.
    """

    def _scope_over(self, frame):
        scope = PicoScope(netname="scope1", pin=1)
        scope._cursors = {"time": (0.0, 1e-3), "volts": None, "channel": "B"}
        scope._capture_for_cursors = lambda timeout=None: frame
        return scope

    def test_it_names_the_channel_and_the_reason(self):
        frame = _FakeFrame(channels=["A"])
        with pytest.raises(UnsupportedScopeFeature) as caught:
            self._scope_over(frame).measure_cursors()

        message = str(caught.value)
        assert "B" in message
        assert "not enabled" in message
        assert "no such channel" not in message

    def test_an_enabled_channel_still_reads(self):
        frame = _FakeFrame(channels=["A", "B"])
        result = self._scope_over(frame).measure_cursors()
        assert result["readings"]["delta_t"] == pytest.approx(1e-3)
        assert "trace_v1" in result["readings"]

    def test_volts_cursors_alone_need_no_channel(self):
        """They are arithmetic, so a disabled channel must not stop them."""
        scope = PicoScope(netname="scope1", pin=1)
        scope._cursors = {"time": None, "volts": (-1.0, 1.0), "channel": "B"}
        scope._capture_for_cursors = lambda timeout=None: pytest.fail(
            "a voltage-only cursor read took a capture")

        assert scope.measure_cursors()["readings"]["delta_v"] == pytest.approx(2.0)


class TestCursorsBelongToTheScopeNotTheChannel:
    """hardware_service builds a driver instance per net.

    A two-channel scope is therefore two PicoScope objects sharing a lock,
    and cursors held on the instance were held per channel. The web UI reads
    them from whichever net it finds first, so a pair placed against the
    second channel was stored somewhere nothing would look. The CLI and the
    page are supposed to be moving one set of markers.
    """

    def _two_nets_on_one_scope(self):
        return (PicoScope(netname="scope1", pin=1),
                PicoScope(netname="scope2", pin=2))

    def test_a_pair_set_on_one_net_is_visible_from_the_other(self):
        first, second = self._two_nets_on_one_scope()
        second.set_cursors(time=(0.0, 1e-3))

        assert first.get_cursors()["time"] == [0.0, 1e-3], (
            "cursors placed against channel B were invisible to the net the "
            "page reads them from")

    def test_clearing_from_either_net_clears_both(self):
        first, second = self._two_nets_on_one_scope()
        first.set_cursors(time=(0.0, 1e-3), volts=(-1.0, 1.0))
        second.clear_cursors()

        assert first.get_cursors()["time"] is None
        assert first.get_cursors()["volts"] is None

    def test_the_channel_read_against_is_carried_with_them(self):
        """The voltages are meaningless without the trace they came from."""
        first, second = self._two_nets_on_one_scope()
        second.set_cursors(time=(0.0, 1e-3))

        assert first.get_cursors()["channel"] == "B"

    def test_a_second_scope_keeps_its_own(self):
        """A bench with two units must not share one pair between them."""
        one = PicoScope(netname="scopeA", pin=1, address="usb::first")
        two = PicoScope(netname="scopeB", pin=1, address="usb::second")

        one.set_cursors(time=(0.0, 1e-3))
        assert two.get_cursors()["time"] is None

    def test_a_fresh_instance_for_the_same_net_still_sees_them(self):
        """The driver is rebuilt on reconnect; the cursors should survive."""
        PicoScope(netname="scope1", pin=1).set_cursors(volts=(-2.0, 2.0))

        assert PicoScope(netname="scope1", pin=1).get_cursors()["volts"] == [-2.0, 2.0]
