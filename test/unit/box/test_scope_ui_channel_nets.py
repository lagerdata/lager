# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
The scope web UI must address the channel a control belongs to, and must not
claim a channel state it never applied.

Two bugs sat behind a live scope showing "Not streaming. Press Connect, then
Start." over a running trace:

* The overlay was hidden with ``element.hidden = true``, but ``.plot-empty``
  sets ``display: grid``, and an author rule outranks the user-agent
  ``[hidden] { display: none }``. The attribute was set and nothing moved.
* A channel is addressed BY net on this box -- each scope net carries a pin
  and the device behind it is bound to that channel -- but every control sent
  to the *selected* net. Channel B's switch drove whichever channel was
  selected, and the initial state (first channel on, rest off) was rendered
  without ever being sent, so the UI could show a channel on while the scope
  had it off. That surfaced as empty captures and "channel X is not enabled"
  from every measurement.

The JS runs under node here for the same reason the command grammar does:
nothing but executing it proves what it sends.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCOPE_DIR = REPO_ROOT / "box" / "lager" / "static" / "scope"
SCOPE_JS = SCOPE_DIR / "scope.js"
SCOPE_CSS = SCOPE_DIR / "scope.css"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to run the scope UI code")


# The one browser global the code under test constructs for itself. Node has
# no DOM, and everything else it touches is handed in by the test.
OPTION_SHIM = """
globalThis.Option = class {
  constructor(text, value) { this.text = text; this.value = value; }
};
"""


def _run_js(body):
    """Run `body` with ScopeApp imported, returning what it JSON-prints."""
    script = "import { ScopeApp } from %s;\n%s\n%s" % (
        json.dumps(str(SCOPE_JS)), OPTION_SHIM, body)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False,
        env=dict(os.environ))
    if result.returncode != 0:
        pytest.fail("node failed: %s" % result.stderr.strip())
    return json.loads(result.stdout)


# A box with one net per channel, as `lager net add` leaves it, in the shape
# /nets/list actually returns (pin is a string there).
TWO_NETS = [{"name": "scope1", "pin": "1"}, {"name": "scope2", "pin": "2"}]


class TestTheOverlayCanActuallyBeHidden:
    """Pins the CSS fix: `hidden` must win over the rule that beat it."""

    def test_a_hidden_rule_exists(self):
        css = SCOPE_CSS.read_text()
        assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none", css), (
            "scope.css has no [hidden] rule, so element.hidden cannot hide "
            "anything that sets its own display")

    def test_the_hidden_rule_outranks_an_id_or_class_display(self):
        css = SCOPE_CSS.read_text()
        rule = re.search(r"\[hidden\]\s*\{[^}]*\}", css).group(0)
        assert "!important" in rule, (
            "a plain [hidden] rule is beaten by any id selector and by any "
            "later class rule that sets display; that is how the 'Not "
            "streaming' overlay stayed up over a running scope")

    def test_the_overlay_still_sets_the_display_that_needed_the_fix(self):
        """If .plot-empty stops setting display, this test has gone stale."""
        css = SCOPE_CSS.read_text()
        block = re.search(r"\.plot-empty\s*\{[^}]*\}", css)
        assert block, ".plot-empty is gone; re-check whether [hidden] is still needed"
        assert "display:" in block.group(0)


@needs_node
class TestAChannelResolvesToItsOwnNet:
    """netForChannel maps by pin, because that is what binds net to channel."""

    def test_each_channel_maps_to_the_net_on_its_pin(self):
        out = _run_js("""
        const f = ScopeApp.prototype.netForChannel;
        const self = { scopeNets: %s };
        process.stdout.write(JSON.stringify({
          a: f.call(self, 0), b: f.call(self, 1),
        }));
        """ % json.dumps(TWO_NETS))
        assert out == {"a": "scope1", "b": "scope2"}

    def test_list_order_does_not_decide_the_channel(self):
        """Nets come back in whatever order the box lists them."""
        reversed_nets = list(reversed(TWO_NETS))
        out = _run_js("""
        const f = ScopeApp.prototype.netForChannel;
        const self = { scopeNets: %s };
        process.stdout.write(JSON.stringify({
          a: f.call(self, 0), b: f.call(self, 1),
        }));
        """ % json.dumps(reversed_nets))
        assert out == {"a": "scope1", "b": "scope2"}

    def test_a_channel_with_no_net_is_unwired_not_someone_elses_net(self):
        """The regression that made channel B's switch drive channel A.

        With only scope2 defined, channel A has nothing addressing it. It
        must come back null so its controls are disabled -- falling back to
        the one net available would point channel A at channel B.
        """
        out = _run_js("""
        const f = ScopeApp.prototype.netForChannel;
        const self = { scopeNets: [{ name: 'scope2', pin: '2' }] };
        process.stdout.write(JSON.stringify({
          a: f.call(self, 0), b: f.call(self, 1),
        }));
        """)
        assert out == {"a": None, "b": "scope2"}

    def test_position_is_used_only_when_no_net_declares_a_pin(self):
        out = _run_js("""
        const f = ScopeApp.prototype.netForChannel;
        const self = { scopeNets: [{ name: 'scopeX' }, { name: 'scopeY' }] };
        process.stdout.write(JSON.stringify({
          a: f.call(self, 0), b: f.call(self, 1), c: f.call(self, 2),
        }));
        """)
        assert out == {"a": "scopeX", "b": "scopeY", "c": None}

    def test_no_nets_means_no_channel_is_addressable(self):
        out = _run_js("""
        const f = ScopeApp.prototype.netForChannel;
        process.stdout.write(JSON.stringify({
          none: f.call({ scopeNets: [] }, 0),
          missing: f.call({}, 0),
        }));
        """)
        assert out == {"none": None, "missing": None}


@needs_node
class TestTheUiAdoptsTheHardwareState:
    """syncChannelState replaces the assumed state with the reported one."""

    # A `this` with two channels wired to their nets, plus stand-ins for the
    # controls syncChannelState writes back to.
    HARNESS = """
    const calls = [];
    const mkSelect = () => ({
      options: [{ value: '0.5' }, { value: '1' }, { value: 'custom' }],
      value: '1',
      add(option, before) {
        this.options.splice(
          before ? this.options.indexOf(before) : this.options.length,
          0, option);
      },
      replaceChildren() { this.options = []; },
      append(...added) { this.options.push(...added); },
    });
    const state = new Map([
      ['A', { enabled: true, voltsPerDiv: 1, attenuation: 1, net: 'scope1',
              toggle: { checked: true }, select: mkSelect(),
              customField: { hidden: false } }],
      ['B', { enabled: false, voltsPerDiv: 1, attenuation: 1, net: 'scope2',
              toggle: { checked: false }, select: mkSelect(),
              customField: { hidden: false } }],
    ]);
    const self = {
      channelState: state,
      capabilities: { voltage_ranges: [
        { full_scale_volts: 0.05 }, { full_scale_volts: 20 }] },
      requestRedraw: ScopeApp.prototype.requestRedraw,
      applyVoltsPerDiv: ScopeApp.prototype.applyVoltsPerDiv,
      showVoltsPerDiv: ScopeApp.prototype.showVoltsPerDiv,
      rebuildScaleChoices: ScopeApp.prototype.rebuildScaleChoices,
      // The real one, not a stub: cursors are read back on connect the same
      // way the scale and the position are, and it has to survive a box that
      // does not know the action.
      refreshCursors: ScopeApp.prototype.refreshCursors,
      adoptCursors: ScopeApp.prototype.adoptCursors,
      runCommand: async () => ({}),
      send: async (action, params, net) => {
        calls.push({ action, net });
        // A 1x probe unless the test says otherwise, so a case about the
        // enable state is not also a case about re-ranging the dropdown.
        if (action === 'get_probe') {
          const answer = REPLY(action, net);
          return answer && answer.probe !== undefined
            ? { value: answer.probe } : { value: 1 };
        }
        return REPLY(action, net);
      },
    };
    """

    def _sync(self, reply_js, report_js):
        return _run_js("""
        %s
        const REPLY = %s;
        await ScopeApp.prototype.syncChannelState.call(self, ['A', 'B']);
        process.stdout.write(JSON.stringify(%s));
        """ % (self.HARNESS, reply_js, report_js))

    def test_a_channel_the_scope_reports_off_is_shown_off(self):
        """The exact bug: UI said channel on, hardware had it off."""
        out = self._sync(
            "(action, net) => action === 'get_net_enabled' "
            "? { value: net === 'scope2' } : { value: 0.5 }",
            "{ a: state.get('A').enabled, b: state.get('B').enabled,"
            "  aBox: state.get('A').toggle.checked,"
            "  bBox: state.get('B').toggle.checked }")
        # Hardware says A off, B on -- the opposite of what was rendered.
        assert out == {"a": False, "b": True, "aBox": False, "bBox": True}

    def test_each_channel_is_asked_about_its_own_net(self):
        out = self._sync(
            "() => ({ value: true })",
            "calls.filter(c => c.action === 'get_net_enabled')"
            "     .map(c => c.net).sort()")
        assert out == ["scope1", "scope2"]

    def test_the_reported_scale_is_adopted(self):
        out = self._sync(
            "(action) => action === 'get_net_enabled' "
            "? { value: true } : { value: 0.5 }",
            "{ perDiv: state.get('A').voltsPerDiv,"
            "  shown: state.get('A').select.value }")
        assert out == {"perDiv": 0.5, "shown": "0.5"}

    def test_an_off_ladder_hardware_scale_is_still_displayed(self):
        """The dropdown must show what the hardware has, ladder or not.

        Leaving the old value showing was its own trap: the sidebar read
        1 V while the trace was drawn at 0.123, and picking the 1 V already
        displayed fired no change event, so the scale looked stuck.
        """
        out = self._sync(
            "(action) => action === 'get_net_enabled' "
            "? { value: true } : { value: 0.123 }",
            "{ perDiv: state.get('A').voltsPerDiv,"
            "  shown: state.get('A').select.value }")
        assert out == {"perDiv": 0.123, "shown": "0.123"}

    def test_an_older_box_without_the_read_leaves_the_ui_alone(self):
        """get_net_enabled is new; a box that rejects it must not blank the UI."""
        out = self._sync(
            "(action) => { if (action === 'get_net_enabled') "
            "  throw new Error('unknown action'); return { value: 0.5 }; }",
            "{ a: state.get('A').enabled, b: state.get('B').enabled }")
        assert out == {"a": True, "b": False}, "should keep the rendered state"

    def test_a_channel_with_no_net_is_not_asked(self):
        out = _run_js("""
        const calls = [];
        const state = new Map([
          ['A', { enabled: true, net: null, toggle: { checked: true } }],
        ]);
        const self = {
          channelState: state,
          send: async (action, params, net) => { calls.push(net); return {}; },
        };
        await ScopeApp.prototype.syncChannelState.call(self, ['A']);
        process.stdout.write(JSON.stringify(calls));
        """)
        assert out == []


@needs_node
class TestControlsTargetTheirOwnNet:
    """send() must honour a per-control net override."""

    def test_send_uses_the_given_net_over_the_selected_one(self):
        out = _run_js("""
        const sent = [];
        globalThis.fetch = async (url, init) => {
          sent.push(JSON.parse(init.body).netname);
          return { ok: true, json: async () => ({}) };
        };
        // A channel net, so an un-overridden per-channel action has one to
        // fall back to; without it `send` would route to the scope net.
        const self = {
          net: 'pico1',
          channelNets: [{ name: 'scope1', pin: 1 }],
          netForAction: ScopeApp.prototype.netForAction,
        };
        await ScopeApp.prototype.send.call(self, 'enable_net', {}, 'scope2');
        await ScopeApp.prototype.send.call(self, 'enable_net', {});
        process.stdout.write(JSON.stringify(sent));
        """)
        assert out == ["scope2", "scope1"], (
            "a per-channel control must reach its own net, and an "
            "un-overridden call must still use the selected one")

    def test_send_without_any_net_is_refused(self):
        out = _run_js("""
        let message = null;
        try {
          await ScopeApp.prototype.send.call(
            { net: null, channelNets: [],
              netForAction: ScopeApp.prototype.netForAction },
            'enable_net', {});
        } catch (e) { message = e.message; }
        process.stdout.write(JSON.stringify(message));
        """)
        assert out and "no scope net" in out


@needs_node
class TestVoltsPerDivOffersTheConventionalSteps:
    """The list was the hardware's range boundaries over four: 13 mV, 1.3 V.

    Nobody reaches for 130 mV/div, and none of the round numbers they do
    reach for were on offer. The daemon takes any volts/div and picks the
    smallest range containing it, so the choices can be the 1-2-5 steps a
    front panel steps through and the hardware can follow.
    """

    # What a 2204A reports: +/- 50 mV through +/- 20 V.
    CAPS = {"voltage_ranges": [
        {"full_scale_volts": v}
        for v in [0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0]
    ]}

    def _choices(self, caps, attenuation):
        return _run_js("""
        const { voltsPerDivChoices } = await import(%s);
        process.stdout.write(JSON.stringify(
          voltsPerDivChoices(%s, %s)));
        """ % (json.dumps(str(SCOPE_JS)), json.dumps(caps), attenuation))

    def test_the_steps_are_all_one_two_or_five(self):
        for attenuation in (1, 10):
            for value in self._choices(self.CAPS, attenuation):
                mantissa = value
                while mantissa < 1:
                    mantissa *= 10
                while mantissa >= 10:
                    mantissa /= 10
                assert round(mantissa, 6) in (1.0, 2.0, 5.0), (
                    "%s V/div is not a 1-2-5 step" % value)

    def test_the_awkward_hardware_values_are_gone(self):
        """0.0125, 0.125 and 1.25 V/div were the old list."""
        offered = self._choices(self.CAPS, 1)
        for awkward in (0.0125, 0.125, 1.25):
            assert awkward not in offered

    def test_a_10x_probe_offers_the_round_numbers_asked_for(self):
        offered = self._choices(self.CAPS, 10)
        for wanted in (0.1, 1, 5, 10):
            assert wanted in offered, "%s V/div missing from %s" % (wanted, offered)

    def test_a_10x_probe_scales_the_whole_list_up(self):
        """Volts/div is at the tip, so a 10x probe reaches ten times as far."""
        at_1x = self._choices(self.CAPS, 1)
        at_10x = self._choices(self.CAPS, 10)
        assert max(at_10x) == max(at_1x) * 10
        assert min(at_10x) > min(at_1x)

    def test_nothing_beyond_the_widest_range_is_offered(self):
        """+/- 20 V fills the screen at 5 V/div; 10 would need a range it lacks."""
        assert max(self._choices(self.CAPS, 1)) == 5

    def test_the_smallest_range_is_reachable_but_not_undershot(self):
        """Below half the smallest range, the driver picks one the unit lacks.

        The +/- 50 mV range fills the screen at 12.5 mV/div. 20 mV/div is
        above half of that and still selects it; 5 mV/div would ask for a
        +/- 20 mV range a 2204A does not have.
        """
        offered = self._choices(self.CAPS, 1)
        assert 0.02 in offered
        assert 0.005 not in offered

    def test_a_scope_reporting_no_ranges_still_gets_a_list(self):
        offered = self._choices({}, 1)
        assert offered and all(v > 0 for v in offered)


@needs_node
class TestAControlChangeRedrawsTheFrameOnScreen:
    """The regression behind "volts/div doesn't do anything".

    draw() runs only when a capture arrives. A control that changes how the
    same samples are drawn had no effect until the next frame, so while
    stopped -- or on a slow trigger -- volts/div could be moved twentyfold
    without the trace shifting a pixel.
    """

    def test_applying_a_scale_marks_the_canvas_for_redraw(self):
        out = _run_js("""
        const state = new Map([['A', { voltsPerDiv: 2.5, net: 'scope1',
          select: { options: [{ value: '2.5' }], value: '2.5',
                    add() {}, },
          customField: { hidden: true } }]]);
        const self = {
          channelState: state, dirty: false,
          requestRedraw: ScopeApp.prototype.requestRedraw,
          showVoltsPerDiv: ScopeApp.prototype.showVoltsPerDiv,
          runCommand: async () => ({}),
        };
        ScopeApp.prototype.applyVoltsPerDiv.call(self, 'A', 0.5);
        process.stdout.write(JSON.stringify({
          dirty: self.dirty, perDiv: state.get('A').voltsPerDiv,
        }));
        """)
        assert out == {"dirty": True, "perDiv": 0.5}, (
            "a scale change must redraw, not wait for the next capture")

    def test_a_readback_does_not_push_the_value_back_to_the_hardware(self):
        """Re-sending what the scope already has would re-range it for nothing."""
        out = _run_js("""
        const sent = [];
        const state = new Map([['A', { voltsPerDiv: 1, net: 'scope1',
          select: { options: [{ value: '1' }], value: '1', add() {} },
          customField: { hidden: true } }]]);
        const self = {
          channelState: state, dirty: false,
          requestRedraw: ScopeApp.prototype.requestRedraw,
          showVoltsPerDiv: ScopeApp.prototype.showVoltsPerDiv,
          runCommand: async (action) => { sent.push(action); return {}; },
        };
        ScopeApp.prototype.applyVoltsPerDiv.call(self, 'A', 0.5, { push: false });
        process.stdout.write(JSON.stringify({ sent, dirty: self.dirty }));
        """)
        assert out["sent"] == [], "a readback must not command the hardware"
        assert out["dirty"] is True, "but it must still redraw"


@needs_node
class TestTheDropdownNeverLiesAboutTheScale:
    """A displayed value the trace is not drawn at is its own dead end.

    With the sidebar reading 2.5 V while the renderer used 1 V, picking the
    2.5 V already shown fires no change event -- so the one correction a user
    would try does nothing.
    """

    HARNESS = """
    const added = [];
    const select = {
      options: [{ value: '0.5' }, { value: '1' }, { value: 'custom' }],
      value: '1',
      add(option, before) {
        added.push({ value: option.value, before: before && before.value });
        this.options.splice(
          before ? this.options.indexOf(before) : this.options.length,
          0, option);
      },
    };
    const state = { voltsPerDiv: 1, select, customField: { hidden: false } };
    """

    def test_an_off_ladder_value_is_added_and_selected(self):
        out = _run_js("""
        %s
        ScopeApp.prototype.showVoltsPerDiv.call({}, state, 0.75);
        process.stdout.write(JSON.stringify({
          added, shown: select.value,
          order: select.options.map(o => o.value),
        }));
        """ % self.HARNESS)
        assert out["shown"] == "0.75"
        # Inserted in order, before the 1 V it sits below.
        assert out["order"] == ["0.5", "0.75", "1", "custom"]

    def test_custom_stays_last_when_the_value_is_the_largest(self):
        out = _run_js("""
        %s
        ScopeApp.prototype.showVoltsPerDiv.call({}, state, 50);
        process.stdout.write(JSON.stringify(select.options.map(o => o.value)));
        """ % self.HARNESS)
        assert out == ["0.5", "1", "50", "custom"], (
            "Custom must remain the last entry")

    def test_an_offered_value_is_selected_without_duplicating_it(self):
        out = _run_js("""
        %s
        ScopeApp.prototype.showVoltsPerDiv.call({}, state, 0.5);
        process.stdout.write(JSON.stringify({
          added, shown: select.value,
          order: select.options.map(o => o.value),
        }));
        """ % self.HARNESS)
        assert out == {"added": [], "shown": "0.5",
                       "order": ["0.5", "1", "custom"]}

    def test_showing_a_value_closes_the_custom_field(self):
        out = _run_js("""
        %s
        ScopeApp.prototype.showVoltsPerDiv.call({}, state, 0.5);
        process.stdout.write(JSON.stringify(state.customField.hidden));
        """ % self.HARNESS)
        assert out is True


@needs_node
class TestTheTriggerLevelIsDrawnWhereItActuallyIs:
    """The Level field had nothing on the plot to represent it.

    You could set 1.02 V against a trace whose peaks never reached it and get
    no hint why nothing triggered. The line has to land on the same scale as
    the trace it is read against, or it is worse than no line at all.
    """

    # 400 px tall, so the centre is 200 and each of the eight divisions is 50.
    HEIGHT = 400

    def _draw(self, level, per_div, source="A", channels=("A", "B")):
        state = "".join(
            "['%s', { voltsPerDiv: %s, net: 'scope%d' }],"
            % (label, per_div if label == source else per_div * 4, i + 1)
            for i, label in enumerate(channels))
        return _run_js("""
        const lines = [];
        const labels = [];
        globalThis.document = {
          documentElement: {},
          getElementById: (id) => ({
            value: id === 'trigger-source' ? %s : %s,
          }),
        };
        globalThis.getComputedStyle = () => ({ getPropertyValue: () => '#0f0' });
        const ctx = {
          save() {}, restore() {}, beginPath() {}, stroke() {},
          setLineDash() {}, moveTo() {}, fillRect() {},
          lineTo(x, y) { lines.push(y); },
          measureText: () => ({ width: 40 }),
          fillText(text) { labels.push(text); },
        };
        const self = { channelState: new Map([%s]) };
        ScopeApp.prototype.drawTriggerLevel.call(self, ctx, 800, %d);
        process.stdout.write(JSON.stringify({ lines, labels }));
        """ % (json.dumps(source), json.dumps(str(level)), state, self.HEIGHT))

    def test_zero_volts_sits_on_the_centre_line(self):
        out = self._draw(0, 1)
        assert out["lines"] == [self.HEIGHT / 2 + 0.5]

    def test_one_division_up_is_one_division_above_centre(self):
        """8 divisions over 400 px: 1 V/div puts 1 V fifty pixels up."""
        out = self._draw(1, 1)
        assert out["lines"] == [self.HEIGHT / 2 - 50 + 0.5]

    def test_a_negative_level_goes_below_centre(self):
        out = self._draw(-2, 1)
        assert out["lines"] == [self.HEIGHT / 2 + 100 + 0.5]

    def test_the_scale_of_the_source_channel_is_the_one_used(self):
        """Channel B here is at 4x A's scale; a level on A must ignore it."""
        at_1 = self._draw(1, 1, source="A")
        at_2 = self._draw(1, 2, source="A")
        # The drawn y carries a half-pixel offset to land on a crisp line, so
        # take it off before comparing distances.
        above = lambda out: self.HEIGHT / 2 - (out["lines"][0] - 0.5)
        # Doubling volts/div halves the level's distance from centre.
        assert above(at_1) == 2 * above(at_2)

    def test_a_level_off_the_top_is_pinned_to_the_top_and_says_so(self):
        """A line drawn off-canvas looks the same as no trigger at all."""
        out = self._draw(99, 1)
        assert out["lines"] == [1.5], "should pin to the top edge"
        assert "\u2191" in out["labels"][0]

    def test_a_level_off_the_bottom_is_pinned_and_points_down(self):
        out = self._draw(-99, 1)
        assert out["lines"] == [self.HEIGHT - 1 + 0.5]
        assert "\u2193" in out["labels"][0]

    def test_an_on_screen_level_is_not_marked_as_clipped(self):
        out = self._draw(1, 1)
        assert "\u2191" not in out["labels"][0]
        assert "\u2193" not in out["labels"][0]

    def test_the_label_names_the_source_channel_and_the_level(self):
        out = self._draw(1.02, 1)
        assert "A" in out["labels"][0]
        assert "1.02" in out["labels"][0]

    def test_a_source_with_no_channel_state_draws_nothing(self):
        out = self._draw(1, 1, source="Z")
        assert out["lines"] == []

    def test_a_blank_level_draws_nothing(self):
        out = _run_js("""
        const lines = [];
        globalThis.document = {
          documentElement: {},
          getElementById: (id) => ({ value: id === 'trigger-source' ? 'A' : '' }),
        };
        globalThis.getComputedStyle = () => ({ getPropertyValue: () => '#0f0' });
        const ctx = { save() {}, restore() {}, beginPath() {}, stroke() {},
          setLineDash() {}, moveTo() {}, fillRect() {}, fillText() {},
          lineTo(x, y) { lines.push(y); },
          measureText: () => ({ width: 40 }) };
        const self = { channelState: new Map([['A', { voltsPerDiv: 1 }]]) };
        ScopeApp.prototype.drawTriggerLevel.call(self, ctx, 800, 400);
        process.stdout.write(JSON.stringify(lines));
        """)
        assert out == [], "an empty field is not a level of zero"


@needs_node
class TestOneBadFrameDoesNotKillTheRenderLoop:
    """tick() schedules the next frame at its end.

    An exception escaping draw() therefore stopped the loop for good: every
    control went dead at once, with a stale trace on screen and nothing to
    say why -- the same symptom as a control that does not work.
    """

    HARNESS = """
    let scheduled = 0;
    globalThis.requestAnimationFrame = () => { scheduled += 1; };
    const errors = [];
    const self = {
      dirty: true, latest: { fake: true },
      console: { error: (m) => errors.push(m) },
      draw() { throw new Error('bad frame'); },
    };
    ScopeApp.prototype.tick.call(self);
    """

    def test_the_next_frame_is_still_scheduled_after_a_failure(self):
        out = _run_js(self.HARNESS + """
        process.stdout.write(JSON.stringify({ scheduled, errors }));
        """)
        assert out["scheduled"] == 1, "the loop must keep going"
        assert "bad frame" in out["errors"][0]

    def test_the_failure_is_reported_once_not_per_frame(self):
        """At 60 fps a per-frame message would bury the console instantly."""
        out = _run_js(self.HARNESS + """
        for (let i = 0; i < 5; i += 1) {
          self.dirty = true;
          ScopeApp.prototype.tick.call(self);
        }
        process.stdout.write(JSON.stringify({ scheduled, errors: errors.length }));
        """)
        assert out["errors"] == 1
        assert out["scheduled"] == 6, "still scheduling every frame"


class TestTheTriggerMarkersCanBeTurnedOff:
    """Point 1 asked for the line to be viewable AND hideable."""

    def test_the_checkbox_exists_and_starts_checked(self):
        html = (SCOPE_DIR / "index.html").read_text()
        assert 'id="trigger-markers"' in html
        marker = html.split('id="trigger-markers"')[1].split('>')[0]
        assert "checked" in marker, "markers should default to visible"

    def test_the_renderer_consults_the_flag(self):
        js = SCOPE_JS.read_text()
        assert "this.showTriggerMarkers" in js
        # And the flag gates both markers, not just the level.
        gated = js.split("if (this.showTriggerMarkers)")[1][:600]
        assert "drawTriggerLevel" in gated
        assert "drawTriggerMarker" in gated


@needs_node
class TestAChannelCanBeMovedUpAndDown:
    """Two traces on one screen overlap until one of them can be moved.

    A view control, and it has to stay one: the hardware's volts offset is
    added into the samples, so driving this from there would move Vmax, Vmin
    and Vavg along with the trace -- readings that no longer describe the
    signal, as the price of making it legible.
    """

    # 400 px tall: centre 200, and each of the eight divisions is 50 px.
    HEIGHT = 400

    def _draw(self, position_div, volts=(1.0,)):
        """Draw one channel at `position_div` and report the y it lands on."""
        return _run_js("""
        const ys = [];
        globalThis.document = {
          documentElement: {},
          getElementById: () => ({ hidden: false, textContent: '' }),
        };
        globalThis.window = { devicePixelRatio: 1 };
        globalThis.getComputedStyle = () => ({ getPropertyValue: () => '#0f0' });
        const ctx = {
          clearRect() {}, beginPath() {}, stroke() {}, save() {}, restore() {},
          setLineDash() {}, fillRect() {}, fillText() {},
          measureText: () => ({ width: 10 }),
          moveTo() {}, lineTo(x, y) { ys.push(y); },
        };
        const frame = {
          channels: [{ channel: 'A' }],
          flags: 0,
          volts: () => %s,
          overflowed: () => false,
        };
        const self = {
          ctx,
          // One pixel wide, so the trace is a single column and its y is
          // unambiguous.
          canvas: { width: 1, height: %d },
          channelState: new Map([['A', { voltsPerDiv: 1, positionDiv: %s }]]),
          showTriggerMarkers: false,
          drawGraticule() {},
        };
        ScopeApp.prototype.draw.call(self, frame);
        process.stdout.write(JSON.stringify(ys));
        """ % (json.dumps(list(volts)), self.HEIGHT, json.dumps(position_div)))

    def test_a_centred_channel_draws_a_volt_one_division_up(self):
        """The unshifted case, so a shift can be measured against it."""
        ys = self._draw(0)
        assert ys == [self.HEIGHT / 2 - 50] * len(ys)

    def test_shifting_up_two_divisions_moves_the_trace_two_divisions_up(self):
        ys = self._draw(2)
        assert ys == [self.HEIGHT / 2 - 50 - 100] * len(ys)

    def test_shifting_down_moves_it_down(self):
        ys = self._draw(-1)
        assert ys == [self.HEIGHT / 2 - 50 + 50] * len(ys)

    def test_the_shift_is_in_divisions_not_volts(self):
        """A division is a division whatever volts/div says.

        Were the offset applied in volts it would scale with the setting, so
        the same shift would move the trace by different amounts at different
        scales -- and pulling two traces apart would undo itself on the next
        range change.
        """
        out = _run_js("""
        const runs = {};
        globalThis.document = {
          documentElement: {},
          getElementById: () => ({ hidden: false, textContent: '' }),
        };
        globalThis.window = { devicePixelRatio: 1 };
        globalThis.getComputedStyle = () => ({ getPropertyValue: () => '#0f0' });
        for (const perDiv of [1, 5]) {
          const ys = [];
          const ctx = {
            clearRect() {}, beginPath() {}, stroke() {}, save() {}, restore() {},
            setLineDash() {}, fillRect() {}, fillText() {},
            measureText: () => ({ width: 10 }),
            moveTo() {}, lineTo(x, y) { ys.push(y); },
          };
          // Zero volts, so only the shift decides where it lands.
          const frame = { channels: [{ channel: 'A' }], flags: 0,
            volts: () => [0], overflowed: () => false };
          const self = {
            ctx, canvas: { width: 1, height: 400 },
            channelState: new Map([['A', { voltsPerDiv: perDiv, positionDiv: 2 }]]),
            showTriggerMarkers: false, drawGraticule() {},
          };
          ScopeApp.prototype.draw.call(self, frame);
          runs[perDiv] = ys[0];
        }
        process.stdout.write(JSON.stringify(runs));
        """)
        assert out["1"] == out["5"] == 200 - 100

    def test_the_trigger_level_moves_with_the_trace_it_belongs_to(self):
        """A level left behind points at the wrong part of the waveform."""
        out = _run_js("""
        const lines = [];
        globalThis.document = {
          documentElement: {},
          getElementById: (id) => ({ value: id === 'trigger-source' ? 'A' : '1' }),
        };
        globalThis.getComputedStyle = () => ({ getPropertyValue: () => '#0f0' });
        const ctx = { save() {}, restore() {}, beginPath() {}, stroke() {},
          setLineDash() {}, moveTo() {}, fillRect() {}, fillText() {},
          lineTo(x, y) { lines.push(y); }, measureText: () => ({ width: 40 }) };
        const self = {
          channelState: new Map([['A', { voltsPerDiv: 1, positionDiv: 2 }]]),
        };
        ScopeApp.prototype.drawTriggerLevel.call(self, ctx, 800, 400);
        process.stdout.write(JSON.stringify(lines));
        """)
        # 1 V is one division up, and the channel is two more: three in all.
        assert out == [200 - 50 - 100 + 0.5]

    @staticmethod
    def _strip_source():
        """The body of buildChannelStrip, anchored on its definition.

        Not the call site a few lines above it, which is what splitting on the
        bare name finds.
        """
        js = SCOPE_JS.read_text()
        body = js.split("buildChannelStrip(label, index, caps) {")[1]
        return body.split("\n  /** Redraw")[0]

    def test_the_control_is_offered_per_channel(self):
        strip = self._strip_source()
        assert "vertical position in divisions" in strip, (
            "each strip needs its own position field")
        assert "VERTICAL_LIMIT" in strip

    def test_moving_a_trace_sends_nothing_to_the_scope(self):
        """The give-away that it is a view control and not a hardware one."""
        applier = self._strip_source().split(
            "const applyPosition")[1].split("};")[0]
        for hardware in ["runCommand", "this.send", "set_offset"]:
            assert hardware not in applier, (
                "%s in applyPosition would move the measurements too" % hardware)
        assert "requestRedraw" in applier


@needs_node
class TestTheWindowCanBeMovedInTime:
    """Signal past the left or right edge was never sampled.

    So unlike the vertical position this cannot be done in the renderer: it
    has to ask the scope for a window somewhere else, which is the
    pre/post-trigger split.
    """

    def _apply(self, divisions, per_div=1e-3):
        """Call applyTimePosition and report what it sent and displayed."""
        return _run_js("""
        const sent = [];
        const shown = { value: null };
        const fields = {
          'time-position': shown,
          timebase: { value: String(%s) },
        };
        globalThis.document = {
          getElementById: (id) => fields[id] || { value: '' },
        };
        const self = {
          dirty: false,
          requestRedraw() { this.dirty = true; },
          runCommand: async (action, params) => { sent.push({ action, params }); },
        };
        await ScopeApp.prototype.applyTimePosition.call(self, %s);
        process.stdout.write(JSON.stringify(
          { sent, shown: shown.value, dirty: self.dirty }));
        """ % (json.dumps(per_div), json.dumps(divisions)))

    def test_a_shift_is_sent_as_seconds_of_the_current_timebase(self):
        """Divisions on the control, seconds on the wire."""
        out = self._apply(2, per_div=1e-3)
        assert out["sent"][0]["action"] == "set_time_offset"
        assert out["sent"][0]["params"]["offset"] == pytest.approx(2e-3)

    def test_looking_back_sends_a_negative_offset(self):
        out = self._apply(-1.5, per_div=1e-3)
        assert out["sent"][0]["params"]["offset"] == pytest.approx(-1.5e-3)

    def test_the_same_divisions_at_a_faster_timebase_is_less_time(self):
        fast = self._apply(2, per_div=1e-6)
        assert fast["sent"][0]["params"]["offset"] == pytest.approx(2e-6)

    def test_the_travel_stops_at_the_edge_of_the_block(self):
        """Past 5 divisions the trigger is off the screen and the split is
        already all-pre or all-post; asking for more cannot be honoured."""
        assert self._apply(99)["shown"] == "5"
        assert self._apply(-99)["shown"] == "-5"

    def test_the_field_shows_what_was_actually_applied(self):
        assert self._apply(2.5)["shown"] == "2.5"

    def test_the_plot_is_redrawn(self):
        assert self._apply(1)["dirty"] is True

    def test_a_readback_does_not_push_the_value_back(self):
        """Adopting the daemon's own offset must not re-arm the scope."""
        out = _run_js("""
        const sent = [];
        const shown = { value: null };
        const fields = { 'time-position': shown, timebase: { value: '1e-3' } };
        globalThis.document = { getElementById: (id) => fields[id] || { value: '' } };
        const self = {
          requestRedraw() {},
          runCommand: async (action) => { sent.push(action); },
        };
        await ScopeApp.prototype.applyTimePosition.call(self, 2, { push: false });
        process.stdout.write(JSON.stringify({ sent, shown: shown.value }));
        """)
        assert out["sent"] == []
        assert out["shown"] == "2", "still shown, just not re-sent"

    def test_the_control_exists_and_is_bounded(self):
        html = (SCOPE_DIR / "index.html").read_text()
        assert 'id="time-position"' in html
        field = html.split('id="time-position"')[1].split(">")[0]
        assert 'min="-5"' in field and 'max="5"' in field
        assert 'id="time-position-reset"' in html

    def test_a_new_timebase_resends_the_position(self):
        """The shift is held in divisions, so a new time/div changes the
        seconds it stands for; left alone the window would stay where the old
        scale put it."""
        js = SCOPE_JS.read_text()
        # Wherever the change handler routes to, setting a timebase has to end
        # up re-sending the position. Asserted on applyTimebase rather than on
        # the listener, so moving the work out of the listener -- which is
        # where the readback made it belong -- is not a failure.
        applier = js.split("async applyTimebase(seconds")[1].split(
            "\n  /** Make the timebase")[0]
        assert "applyTimePosition" in applier
        handler = js.split("el('timebase').addEventListener")[1].split("});")[0]
        assert "applyTimebase" in handler


class TestTheChannelStripHasCouplingAndProbe:
    """The two per-channel settings that were console-only.

    Both change what the trace means rather than how it is drawn, and the
    probe ratio is what decides which volts/div settings the unit can reach,
    so leaving it off the panel left the scale list depending on a value
    nothing on screen showed.
    """

    @staticmethod
    def _strip_source():
        js = SCOPE_JS.read_text()
        body = js.split("buildChannelStrip(label, index, caps) {")[1]
        return body.split("\n  /** Redraw")[0]

    def test_both_controls_are_offered_per_channel(self):
        strip = self._strip_source()
        assert "set_coupling" in strip
        assert "state.probeSelect" in strip
        assert "applyProbe" in strip

    def test_ground_coupling_is_not_offered(self):
        """The hardware has no ground switch and the daemon refuses it, so a
        control for it could only ever produce an error."""
        js = SCOPE_JS.read_text()
        couplings = js.split("const COUPLINGS = ")[1].split(";")[0]
        assert "'dc'" in couplings and "'ac'" in couplings
        assert "gnd" not in couplings.lower()

    def test_an_unwired_channel_can_change_neither(self):
        """The same trap the other controls had: with no net of its own they
        fall through to the selected net and drive the wrong channel."""
        disabled = self._strip_source().split("control.disabled = true")[0]
        tail = disabled[-400:]
        assert "couplingSelect" in tail and "probeSelect" in tail

    def test_changing_the_probe_rereads_the_scale(self):
        """Attenuation changes which range a volts/div maps onto, so the value
        the panel shows has to come back from the hardware rather than being
        assumed to have survived."""
        js = SCOPE_JS.read_text()
        applier = js.split("async applyProbe(label, ratio")[1].split(
            "\n  /** Make a channel's probe")[0]
        assert "set_probe" in applier
        assert "rebuildScaleChoices" in applier
        assert "get_scale" in applier

    def test_the_probe_dropdown_never_lies(self):
        """A ratio set from the console -- a current clamp, say -- has to
        appear rather than leaving the control showing something else."""
        shown = _run_js("""
        const app = Object.create(ScopeApp.prototype);
        const options = [];
        const select = {
          options,
          value: '1',
          add(option, before) {
            const at = before ? options.indexOf(before) : options.length;
            options.splice(at < 0 ? options.length : at, 0, option);
          },
        };
        const state = { probeSelect: select };
        app.showProbe(state, 20);
        process.stdout.write(JSON.stringify({
          value: select.value,
          order: options.map((o) => Number(o.value)),
        }));
        """)
        assert shown["value"] == "20"
        assert shown["order"] == sorted(shown["order"]), (
            "an inserted ratio has to keep the list in order")


class TestTheMeasurementPanelIsLiveAndComplete:
    """It showed five of the fourteen quantities, read once, on Start."""

    def test_every_quantity_the_daemon_computes_is_listed(self):
        """The keys are the daemon's own field names, so a rename there shows
        up here as a dash rather than as a missing row nobody notices."""
        js = SCOPE_JS.read_text()
        listed = set(re.findall(r"\['[^']+', '(\w+)', ", js))
        from_daemon = {
            "vmax", "vmin", "vpp", "vavg", "vrms", "overshoot",
            "period", "frequency", "rise_time", "fall_time",
            "pulse_width_positive", "pulse_width_negative",
            "duty_cycle_positive", "duty_cycle_negative",
        }
        assert from_daemon <= listed, (
            "missing from the panel: %s" % sorted(from_daemon - listed))

    def test_the_whole_set_comes_from_one_request(self):
        """Thirteen actions meant thirteen captures, and values from different
        moments of a live signal: vpp need not have equalled vmax - vmin."""
        js = SCOPE_JS.read_text()
        refresh = js.split("async refreshMeasurements()")[1].split(
            "\n  /** Keep the measurement")[0]
        assert "measure_all" in refresh
        for one_at_a_time in ["measure_vpp", "measure_vmax", "measure_vrms"]:
            assert one_at_a_time not in refresh

    def test_an_absent_quantity_is_not_shown_as_zero(self):
        """A DC level has no period. Zero would read as a measurement."""
        js = SCOPE_JS.read_text()
        refresh = js.split("async refreshMeasurements()")[1].split(
            "\n  /** Keep the measurement")[0]
        assert "=== undefined" in refresh or "undefined ===" in refresh
        assert "\\u2014" in refresh

    def test_running_polls_and_stopping_does_not(self):
        js = SCOPE_JS.read_text()
        start = js.split("el('btn-start').addEventListener")[1].split("});")[0]
        assert "startMeasurementPolling" in start
        stop = js.split("el('btn-stop').addEventListener")[1].split("});")[0]
        assert "stopMeasurementPolling" in stop

    def test_disconnecting_stops_the_timer(self):
        """Otherwise it outlives the socket, taking a capture every half
        second against a scope nobody is watching."""
        js = SCOPE_JS.read_text()
        disconnect = js.split("  disconnect() {")[1].split("\n  }")[0]
        assert "stopMeasurementPolling" in disconnect
        # Before the early return, or a page that never connected leaks it.
        assert disconnect.index("stopMeasurementPolling") < disconnect.index(
            "if (!this.socket) return;")


class TestTheTimebaseComesFromTheHardware:
    """The list was a fixed 1 us to 100 ms whatever was attached.

    Same fault the volts/div list had before it was derived: it offered
    settings the unit cannot reach, and never read back, so choosing one
    silently got you whatever the hardware rounded to.
    """

    # A 2204A: 100 MS/s, and the driver's floor of 8000 samples a block.
    CAPS = {"max_sample_rate_hz": 1e8}
    DEPTH = 8000

    def _choices(self, caps, depth):
        return _run_js("""
        const { timebaseChoices } = await import(%s);
        process.stdout.write(JSON.stringify(timebaseChoices(%s, %s)));
        """ % (json.dumps(str(SCOPE_JS)), json.dumps(caps), json.dumps(depth)))

    def test_the_unreachable_fast_steps_are_dropped(self):
        """8000 samples at 10 ns is 80 us of signal, which is 8 us across ten
        divisions. Anything faster cannot be captured."""
        offered = self._choices(self.CAPS, self.DEPTH)
        assert min(offered) >= 8e-6
        for impossible in (1e-6, 2e-6, 5e-6):
            assert impossible not in offered

    def test_the_reachable_ones_are_kept(self):
        offered = self._choices(self.CAPS, self.DEPTH)
        for wanted in (1e-5, 1e-4, 1e-3, 1e-2, 1e-1):
            assert wanted in offered, "%s s/div is reachable" % wanted

    def test_an_unknown_unit_is_offered_everything(self):
        """Before the first capture there is no depth, and refusing to offer a
        timebase until one arrives would leave nothing to capture with."""
        assert self._choices({}, None) == self._choices(self.CAPS, None)
        assert 1e-6 in self._choices({}, None)

    def test_a_faster_unit_offers_more(self):
        """The derivation has to follow the hardware, not just clamp it."""
        slow = self._choices({"max_sample_rate_hz": 1e6}, self.DEPTH)
        fast = self._choices({"max_sample_rate_hz": 1e9}, self.DEPTH)
        assert min(fast) < min(slow)

    def test_setting_a_timebase_reads_back_what_the_unit_did(self):
        js = SCOPE_JS.read_text()
        applier = js.split("async applyTimebase(seconds")[1].split(
            "\n  /** Make the timebase")[0]
        assert "set_timebase" in applier
        assert "get_timebase" in applier
        assert "showTimebase" in applier

    def test_the_dropdown_shows_a_rounded_value_it_never_offered(self):
        """1 ms/div on a 2204A is really 1.024 ms/div. Showing the request
        would be the control reading back its own input."""
        shown = _run_js("""
        const app = Object.create(ScopeApp.prototype);
        const options = [
          { value: '0.001' }, { value: '0.002' }, { value: '0.005' },
        ];
        const select = {
          options,
          value: '0.001',
          add(option, before) {
            const at = before ? options.indexOf(before) : options.length;
            options.splice(at < 0 ? options.length : at, 0, option);
          },
        };
        globalThis.document = { getElementById: () => select };
        app.showTimebase(0.001024);
        process.stdout.write(JSON.stringify({
          value: select.value,
          order: options.map((o) => Number(o.value)),
        }));
        """)
        assert shown["value"] == "0.001024"
        assert shown["order"] == sorted(shown["order"])

    def test_the_capture_depth_trims_the_list(self):
        """Depth comes from a frame, not the capabilities: it is what the
        daemon chose, and it is half of what bounds the fastest screen."""
        js = SCOPE_JS.read_text()
        stats = js.split("updateStats(frame) {")[1].split("\n  }")[0]
        assert "rebuildTimebaseChoices" in stats
        assert "samplesPerChannel" in stats


class TestTheBoxCanReportChannelState:
    """The UI's readback needs a handler action behind it."""

    def test_get_net_enabled_is_handled(self):
        from lager.http_handlers import net_command

        class _Scope:
            def is_channel_enabled(self):
                return True

        original = net_command._proxy
        net_command._proxy = lambda *a, **k: _Scope()
        try:
            result = net_command._scope("scope1", "scope-channel", "get_net_enabled", {})
        finally:
            net_command._proxy = original

        assert result["value"] is True
        assert "scope1" in result["message"]

    def test_a_disabled_channel_reports_false(self):
        from lager.http_handlers import net_command

        class _Scope:
            def is_channel_enabled(self):
                return False

        original = net_command._proxy
        net_command._proxy = lambda *a, **k: _Scope()
        try:
            result = net_command._scope("scope2", "scope-channel", "get_net_enabled", {})
        finally:
            net_command._proxy = original

        assert result["value"] is False
        assert "disabled" in result["message"]


class TestTheBoxCanReportEveryMeasurementAtOnce:
    """The panel's fourteen readouts needed fourteen requests without this.

    Each per-quantity action takes its own capture, so a panel built from them
    cost a capture apiece and mixed moments of a live signal together -- a set
    that need not agree with itself.
    """

    @staticmethod
    def _call(measurements):
        from lager.http_handlers import net_command

        class _Scope:
            def measure_all(self):
                return measurements

        original = net_command._proxy
        net_command._proxy = lambda *a, **k: _Scope()
        try:
            return net_command._scope("scope1", "scope-channel", "measure_all", {})
        finally:
            net_command._proxy = original

    def test_the_whole_set_comes_back(self):
        values = {"vpp": 1.5, "vmax": 1.0, "vmin": -0.5, "frequency": 60.0}
        result = self._call(values)
        assert result["value"] == values
        assert "4 measurement" in result["message"]

    def test_the_message_carries_the_values_and_their_units(self):
        """The console and the CLI both print the message, so a bare count is
        no answer to somebody who typed `measure all`."""
        result = self._call({"vpp": 1.5, "frequency": 60.0, "duty_cycle_positive": 50.0})
        assert "vpp 1.5 V" in result["message"]
        assert "frequency 60 Hz" in result["message"]
        # The bulk keys are the daemon's long spellings, not the short forms
        # the single-measurement actions take, so the units are a separate map.
        assert "duty_cycle_positive 50 %" in result["message"]

    def test_a_capture_with_nothing_resolvable_is_not_an_error(self):
        """A flat DC level has no period, no duty cycle and no edges. An empty
        set is an answer; raising would read as a broken scope."""
        result = self._call({})
        assert result["value"] == {}
        assert "0 measurement" in result["message"]

    def test_the_values_are_the_ones_the_panel_asks_for(self):
        """Pins the two halves together: the UI indexes this dict by the
        daemon's field names, so the wire keys are the contract."""
        js = SCOPE_JS.read_text()
        listed = set(re.findall(r"\['[^']+', '(\w+)', ", js))
        result = self._call({"duty_cycle_positive": 50.0, "pulse_width_positive": 1e-3})
        for key in result["value"]:
            assert key in listed, "%s comes back but is never displayed" % key
