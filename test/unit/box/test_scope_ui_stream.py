# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
How the web scope keeps its stream and its controls in step with the box
(box/lager/static/scope/scope.js).

The stream is paced by credit: the daemon sends a frame only against one the
page has handed back, and the page hands one back per frame it received by
the time it draws. Before that the daemon pushed every capture, a slow link
queued them, and the trace fell seconds behind and froze. The controls
follow the state the daemon pushes after every change, so a setting made
from the terminal shows on a page that is already open.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCOPE_DIR = REPO_ROOT / "box" / "lager" / "static" / "scope"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to run the scope UI code")

PRELUDE = """
import { ScopeApp, consoleLogHeight, consoleLogStep } from %s;
import * as render from %s;
globalThis.Option = class {
  constructor(text, value) { this.text = text; this.value = value; }
};
function socket(readyState = 1) {
  return { readyState, sent: [], send(text) { this.sent.push(JSON.parse(text)); } };
}
function app(extra = {}) {
  return Object.assign(Object.create(ScopeApp.prototype), {
    owedCredits: 0,
    clockOffsets: [],
    console: { lines: [], write(text, kind) { this.lines.push([kind, text]); },
               error(text) { this.lines.push(['error', text]); } },
  }, extra);
}
""" % (json.dumps(str(SCOPE_DIR / "scope.js")), json.dumps(str(SCOPE_DIR / "render.js")))


def _height(expr):
    return _run_js("process.stdout.write(JSON.stringify(%s));" % expr)


def _run_js(body):
    script = PRELUDE + body
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False, env=dict(os.environ))
    if result.returncode != 0:
        pytest.fail("node failed: %s" % result.stderr.strip())
    return json.loads(result.stdout)


class TestConsoleResize:

    def test_pulling_the_edge_up_makes_the_log_taller(self):
        assert _height("consoleLogHeight(150, 80, 600)") == 230

    def test_the_log_cannot_cover_the_waveform(self):
        assert _height("consoleLogHeight(150, 5000, 400)") == 400

    def test_the_log_keeps_a_few_lines(self):
        assert _height("consoleLogHeight(150, -5000, 400)") == 48

    def test_a_short_window_still_leaves_the_minimum(self):
        assert _height("consoleLogHeight(150, 0, 10)") == 48

    def test_an_unmeasured_window_does_not_collapse_the_log(self):
        assert _height("consoleLogHeight(150, 80, Infinity)") == 230

    def test_a_key_step_down_from_an_overlong_drag_moves_at_once(self):
        # Stepped from the height on screen, not the 5000 px the drag asked
        # for, which took a keypress per 16 px of the excess.
        assert _height("consoleLogStep(5000, -16, 400)") == 384


# A browser's WebSocket and DOM, enough for connect() and disconnect().
TRANSPORT = """
const sockets = [];
globalThis.WebSocket = class {
  constructor(url) {
    Object.assign(this, { url: String(url), readyState: 0, listeners: {}, sent: [], closed: false });
    sockets.push(this);
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type) { for (const fn of this.listeners[type] || []) fn({}); }
  send(text) {
    if (this.readyState !== 1) throw new Error('InvalidStateError');
    this.sent.push(JSON.parse(text));
  }
  close() { this.closed = true; this.readyState = 3; }
};
globalThis.window = { location: { href: 'http://box:9000/scope', protocol: 'http:' } };
const elements = {};
globalThis.document = { getElementById: (id) => (elements[id] ||= { textContent: '', hidden: false }) };
// A page whose ticket requests wait to be answered, in order.
let answer;
const page = app({
  net: 'pico1', connectAttempt: 0, connecting: false, socket: null, refreshHz: 60,
  overlayHidden: true, setLink() {}, stopMeasurementPolling() {}, resetRollDelay() {},
  loadCapabilities() { return new Promise((resolve) => { answer = resolve; }); },
});
const ticket = () => answer({ ws_path: '/scope/pico1/ws?token=t' });
"""


class TestConnecting:
    """One stream per page, however Connect and Disconnect are pressed."""

    def _run(self, body):
        return _run_js(TRANSPORT + body)

    def test_a_second_press_while_connecting_opens_nothing_more(self):
        out = self._run("""
        const first = page.connect();
        const second = page.connect();
        ticket();
        await Promise.all([first, second]);
        process.stdout.write(JSON.stringify(sockets.map((s) => s.url)));
        """)
        assert out == ["ws://box:9000/scope/pico1/ws?token=t"]

    def test_disconnecting_while_the_ticket_is_out_opens_nothing(self):
        out = self._run("""
        const pending = page.connect();
        page.disconnect();
        ticket();
        await pending;
        process.stdout.write(JSON.stringify({
          sockets: sockets.length, connecting: page.connecting,
          button: elements.connect.textContent, overlay: !elements['plot-empty'].hidden }));
        """)
        assert out == {"sockets": 0, "connecting": False, "button": "Connect", "overlay": True}

    def test_a_socket_still_opening_is_closed_without_a_send_that_would_throw(self):
        out = self._run("""
        const pending = page.connect();
        ticket();
        await pending;
        page.disconnect();
        process.stdout.write(JSON.stringify({ sent: sockets[0].sent, closed: sockets[0].closed }));
        """)
        assert out == {"sent": [], "closed": True}

    def test_a_replaced_sockets_late_close_leaves_the_new_one_connected(self):
        out = self._run("""
        let pending = page.connect();
        ticket();
        await pending;
        page.disconnect();
        pending = page.connect();
        ticket();
        await pending;
        const [old, current] = sockets;
        current.readyState = 1;
        current.emit('open');
        old.emit('close');
        process.stdout.write(JSON.stringify({
          connected: page.socket === current, button: elements.connect.textContent,
          subscribed: current.sent.map((m) => m.command) }));
        """)
        assert out == {"connected": True, "button": "Disconnect", "subscribed": ["Subscribe"]}

    def test_the_stream_ending_puts_the_overlay_back_and_drops_owed_credit(self):
        out = self._run("""
        page.overlayHidden = true;
        page.owedCredits = 3;
        page.pendingBuffer = new ArrayBuffer(8);
        page.streamEnded();
        process.stdout.write(JSON.stringify({
          overlay: !elements['plot-empty'].hidden, owed: page.owedCredits,
          pending: page.pendingBuffer }));
        """)
        assert out == {"overlay": True, "owed": 0, "pending": None}

    def test_the_rate_reads_zero_once_nothing_more_is_coming(self):
        # Recomputed only as frames arrive, so it went on showing the last
        # rate measured after Stop and after Disconnect.
        out = self._run("""
        const stat = () => elements['stat-rate'].textContent;
        page.rate = 27;
        page.fps = 27;
        elements['stat-rate'] = { textContent: '27 cap/s \u00b7 27 fps' };
        elements['trig-status'] = {};
        page.state = { acquiring: false };
        page.status = 'trigd';
        page.updateTriggerStatus({ flags: 1, streaming: false });
        const stopped = stat();
        elements['stat-rate'].textContent = '27 cap/s';
        page.streamEnded();
        process.stdout.write(JSON.stringify({ stopped, ended: stat(), rate: page.rate }));
        """)
        assert out == {"stopped": "0 cap/s", "ended": "0 cap/s", "rate": 0}


class TestCredits:

    def test_every_frame_received_is_returned_in_one_message(self):
        sent = _run_js("""
        const s = socket();
        const a = app({ socket: s });
        for (let i = 0; i < 3; i++) a.onCapture(new ArrayBuffer(8));
        a.returnCredits();
        a.returnCredits();
        process.stdout.write(JSON.stringify(s.sent));
        """)
        assert sent == [{"command": "Credit", "count": 3}]

    def test_only_the_newest_frame_is_kept_for_drawing(self):
        kept = _run_js("""
        const a = app({ socket: socket() });
        const first = new ArrayBuffer(8), second = new ArrayBuffer(16);
        a.onCapture(first);
        a.onCapture(second);
        process.stdout.write(JSON.stringify(a.pendingBuffer.byteLength));
        """)
        assert kept == 16

    def test_nothing_is_sent_on_a_socket_that_is_closing(self):
        out = _run_js("""
        const s = socket(2);
        const a = app({ socket: s });
        a.onCapture(new ArrayBuffer(8));
        a.returnCredits();
        process.stdout.write(JSON.stringify({ sent: s.sent, owed: a.owedCredits }));
        """)
        assert out == {"sent": [], "owed": 1}

    def test_a_slow_link_is_given_credit_for_its_round_trip(self):
        out = _run_js("""
        const s = socket();
        const a = app({ socket: s, streamFps: 60, subscribedAt: performance.now() - 150 });
        a.onControlMessage(JSON.stringify({ Response: { response: 'Subscribed' } }));
        process.stdout.write(JSON.stringify({ sent: s.sent, floor: render.CREDIT_WINDOW }));
        """)
        (message,) = out["sent"]
        assert message["command"] == "Credit"
        # 150 ms at 60 Hz needs 12 or so in flight, against the 6 subscribed with.
        assert out["floor"] + message["count"] >= 12

    def test_a_fast_link_keeps_the_credit_it_subscribed_with(self):
        sent = _run_js("""
        const s = socket();
        const a = app({ socket: s, streamFps: 60, subscribedAt: performance.now() });
        a.onControlMessage(JSON.stringify({ Response: { response: 'Subscribed' } }));
        process.stdout.write(JSON.stringify(s.sent));
        """)
        assert sent == []

    def test_dropped_captures_are_a_note_and_a_refusal_is_an_error(self):
        lines = _run_js("""
        const a = app({ socket: socket() });
        a.onControlMessage(JSON.stringify({ Response: { response: 'Error', message: 'dropped 4 captures' } }));
        a.onControlMessage(JSON.stringify({ Response: { response: 'Error', message: 'level out of range' } }));
        process.stdout.write(JSON.stringify(a.console.lines));
        """)
        assert lines == [["note", "dropped 4 captures"], ["error", "level out of range"]]


class TestRollDelay:
    """A rolling screen is drawn as far behind live as its stream runs late."""

    def test_the_delay_is_learned_from_how_late_frames_arrive(self):
        target = _run_js("""
        const a = app({});
        a.resetRollDelay();
        // Box frames every 50 ms, arriving 20 ms late now and then.
        const lateness = [0, 0, 20, 0, 5, 0];
        lateness.forEach((late, i) => {
          const end = 1000 + 50 * i;
          a.noteArrival({ streaming: true, captureMonoNs: end * 1e6 }, 5000 + end + late);
        });
        process.stdout.write(JSON.stringify(a.rollTarget));
        """)
        # 50 ms between frames plus the 20 ms late one, and the margin.
        assert target == pytest.approx(70 + 15)

    def test_a_block_ends_the_rolling_account(self):
        out = _run_js("""
        const a = app({});
        a.resetRollDelay();
        a.noteArrival({ streaming: true, captureMonoNs: 1e9 }, 5000);
        a.noteArrival({ streaming: false, captureMonoNs: 2e9 }, 6000);
        process.stdout.write(JSON.stringify(a.rollLastEnd));
        """)
        assert out is None

    def test_the_delay_is_no_more_than_the_history_reaches(self):
        view = _run_js("""
        const a = app({ clockOffset: 0 });
        a.resetRollDelay();
        a.rollDelay = 400;
        a.rollTarget = 400;
        const realNow = performance.now();
        // 1 ms pairs: 100 pairs of history is 100 ms, less than the 400 wanted.
        const frame = { samplesPerChannel: 2200, screen: 2000,
                        sampleIntervalNs: 0.5e6, captureMonoNs: realNow * 1e6 };
        process.stdout.write(JSON.stringify(a.rollView(frame)));
        """)
        # Not before the history; the few samples past it are the time the
        # test took to get here.
        assert 0 <= view["start"] < 20
        assert view["end"] - view["start"] == 2000

    def _views(self, steps):
        """Roll views of an old frame, then a new one, under each scope state."""
        return _run_js("""
        const a = app({ clockOffset: 0, socket: socket() });
        a.resetRollDelay();
        a.rollDelay = 0;
        a.rollTarget = 0;
        const frame = (agoMs) => ({ samplesPerChannel: 2200, screen: 2000, sampleIntervalNs: 0.5e6,
                                    captureMonoNs: (performance.now() - agoMs) * 1e6 });
        const old = frame(5000);
        const views = [];
        for (const [acquiring, fresh] of %s) {
          a.state = { acquiring };
          views.push(a.rollView(fresh ? frame(0) : old));
        }
        process.stdout.write(JSON.stringify(views));
        """ % json.dumps(steps))

    def test_a_stopped_roll_rests_on_its_newest_samples(self):
        # Drawn against the clock, a stopped screen went on scrolling into
        # blank, since nothing more would arrive to fill the right edge.
        (stopped,) = self._views([[False, False]])
        assert stopped == {"start": 200, "end": 2200}

    def test_a_running_roll_keeps_moving(self):
        (running,) = self._views([[True, False]])
        assert running["end"] > 2200

    def test_run_moves_it_on_only_when_a_newer_frame_arrives(self):
        stopped, resumed, fresh = self._views([[False, False], [True, False], [True, True]])
        assert stopped == resumed == {"start": 200, "end": 2200}
        assert fresh != stopped


STATE = {
    "acquiring": True, "rolling": False, "capture_mode": "auto",
    "trigger": {"source": {"Alphabetic": "B"}, "level": 0.25, "slope": "falling",
                "holdoff_s": 0.001, "position_percent": 50},
    "timebase": {"time_per_div": 1e-3, "time_offset": 0, "roll": "auto"},
    "acquisition": {"mode": "average", "average_count": 64},
    "channels": [{"channel": {"Alphabetic": "A"}, "enabled": False, "volts_per_div": 1,
                  "volts_offset": 0, "coupling": "DC", "attenuation": 1}],
    "display": {"persistence": 2, "math": {"expr": "A-B"}},
}


def _apply(states, editing=None):
    """Apply each state in turn; return the fields and what else changed."""
    return _run_js("""
    const fields = {};
    const field = (id) => (fields[id] = fields[id] || { id, value: '', checked: false });
    globalThis.document = { getElementById: field, activeElement: null };
    const editing = %s;
    if (editing) document.activeElement = field(editing);
    const toggle = { checked: true };
    const a = app({
      channelState: new Map([['A', { enabled: true, toggle, attenuation: 1, voltsPerDiv: 1 }]]),
      timePositionDiv: 0,
      adoptCursors() {}, showTimebase() {}, applyTimePosition() {},
      showProbe() {}, rebuildScaleChoices() {}, requestRedraw() {},
      applyVoltsPerDiv(label, v) { this.channelState.get(label).voltsPerDiv = v; },
    });
    const dropped = [];
    for (const state of %s) {
      a.persist = { layer: true };
      a.applyState(state);
      dropped.push(a.persist === null);
    }
    const values = {};
    for (const [id, f] of Object.entries(fields)) values[id] = f.id === 'display-xy' ? f.checked : f.value;
    process.stdout.write(JSON.stringify({ values, dropped, toggle: toggle.checked }));
    """ % (json.dumps(editing), json.dumps(states)))


class TestPushedState:

    def test_the_controls_follow_the_state(self):
        out = _apply([STATE])
        values = out["values"]
        assert values["trigger-mode"] == "auto"
        assert values["trigger-source"] == "B"
        assert values["trigger-slope"] == "falling"
        assert values["trigger-level"] == "0.25"
        assert values["trigger-holdoff"] == "0.001"
        assert values["acquire-mode"] == "average"
        assert values["acquire-count"] == "64"
        assert values["roll-mode"] == "auto"
        assert values["display-persistence"] == "2"
        assert values["display-math"] == "A-B"
        assert values["display-fft"] == "off"
        assert out["toggle"] is False

    def test_a_field_being_typed_in_is_left_alone(self):
        out = _apply([STATE], editing="trigger-level")
        assert out["values"]["trigger-level"] == ""
        assert out["values"]["trigger-slope"] == "falling"

    def test_a_new_scale_drops_the_persistence_layer_and_the_same_state_keeps_it(self):
        rescaled = json.loads(json.dumps(STATE))
        rescaled["channels"][0]["volts_per_div"] = 0.5
        out = _apply([STATE, STATE, rescaled])
        # The first has nothing to compare with; the repeat changes nothing.
        assert out["dropped"] == [False, False, True]

    def test_a_new_timebase_drops_the_persistence_layer(self):
        slower = json.loads(json.dumps(STATE))
        slower["timebase"]["time_per_div"] = 2e-3
        assert _apply([STATE, slower])["dropped"] == [False, True]


class TestTheUnitGoingAndComing:
    """An unplug must not leave the page looking as if it is still live."""

    def _run(self, body, states):
        return _run_js("""
        const fields = {};
        globalThis.document = {
          getElementById: (id) => (fields[id] = fields[id] || { id, value: '',
            checked: false, hidden: false, innerHTML: '', textContent: '' }),
        };
        const stream = { readyState: 1, sent: [], send(text) { this.sent.push(JSON.parse(text)); } };
        const a = app({
          channelState: new Map([['A', { enabled: true, toggle: { checked: true },
            attenuation: 1, voltsPerDiv: 1 }]]),
          timePositionDiv: 0, overlayHidden: true, owedCredits: 3,
          pendingBuffer: 'held', persist: { layer: true }, socket: stream,
          adoptCursors() {}, showTimebase() {}, applyTimePosition() {},
          showProbe() {}, rebuildScaleChoices() {}, requestRedraw() {},
          applyVoltsPerDiv() {},
        });
        const notes = [];
        a.console.write = (text, kind) => notes.push([kind, text]);
        for (const state of %s) a.applyState(state);
        process.stdout.write(JSON.stringify({
          overlay: !fields['plot-empty'].hidden,
          hint: fields['plot-empty-hint'].innerHTML,
          owed: a.owedCredits, pending: a.pendingBuffer, persist: a.persist,
          notes, subscribed: stream.sent.map((m) => m.command),
        }));
        """ % json.dumps(states) + body)

    def test_an_empty_channel_list_is_an_unplug(self):
        gone = json.loads(json.dumps(STATE))
        gone["channels"] = []
        gone["acquiring"] = False
        out = self._run("", [STATE, gone])
        assert out["overlay"] is True
        assert "unplugged" in out["hint"]
        assert out["owed"] == 0
        assert out["pending"] is None
        assert out["persist"] is None
        assert ["note", "The scope was unplugged."] in out["notes"]
        assert out["subscribed"] == []

    def test_the_scope_coming_back_asks_for_the_stream_again(self):
        gone = json.loads(json.dumps(STATE))
        gone["channels"] = []
        gone["acquiring"] = False
        out = self._run("", [STATE, gone, STATE])
        assert "back" in out["hint"]
        assert ["note", "The scope is back."] in out["notes"]
        assert out["subscribed"] == ["Subscribe"]
        assert out["overlay"] is True
