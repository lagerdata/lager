# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
The scope page's position controls: how the arrows step them, from the keys
or from the buttons inside each field, when a typed value takes effect, and
what the console says about them.

The two positions differ on purpose. Vertical never leaves the page, so it is
applied as it is typed; horizontal re-arms the scope for a fresh capture, so
a typed value waits for Enter. The arrows apply at once on both.
"""
from __future__ import annotations

import html.parser
import json
import os
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCOPE_JS = REPO_ROOT / "box" / "lager" / "static" / "scope" / "scope.js"
INDEX_HTML = REPO_ROOT / "box" / "lager" / "static" / "scope" / "index.html"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to run the scope UI code")


def _run_js(body):
    script = """
    globalThis.Option = class {
      constructor(text, value) { this.text = text; this.value = value; }
    };
    import * as scope from %s;
    %s
    """ % (json.dumps(str(SCOPE_JS)), body)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False,
        env=dict(os.environ))
    if result.returncode != 0:
        pytest.fail("node failed: %s" % result.stderr.strip())
    return json.loads(result.stdout)


def _steps(cases):
    """stepPosition over `[text, direction, maxStep or None]` cases."""
    return _run_js("""
    const cases = %s;
    process.stdout.write(JSON.stringify(cases.map(([text, dir, max]) =>
      scope.stepPosition(text, dir, max === null ? Infinity : max))));
    """ % json.dumps(cases))


class TestArrowStepping:
    """The step is the last digit written; only a bare 0 takes it from the scale."""

    def test_the_last_digit_is_the_one_stepped(self):
        assert _steps([["1.15", 1, None], ["1.1", 1, None], ["1.1", -1, None],
                       ["1.14", -1, None]]) == ["1.16", "1.2", "1.0", "1.13"]

    def test_a_carry_keeps_the_places(self):
        """1.20, not 1.2: the next press must still step by 0.01."""
        assert _steps([["1.19", 1, None], ["0.99", 1, None], ["-0.01", 1, None]]) == [
            "1.20", "1.00", "0.00"]

    def test_no_float_noise(self):
        assert _steps([["0.29", 1, None], ["0.7", -1, None]]) == ["0.30", "0.6"]

    def test_the_scale_leaves_a_written_digit_alone(self):
        """At 0.5 V/div a tenth of a division is 0.05 V, finer than the 0.1
        that "1.1" is written to; the step is still 0.1."""
        out = _run_js("""
        const at = (perDiv) => scope.scaleStep(perDiv);
        process.stdout.write(JSON.stringify([
          scope.stepPosition('1.1', 1, at(0.5)),
          scope.stepPosition('1.14', -1, at(0.01)),
          scope.stepPosition('0.04', -1, at(0.01)),
          // A zero written to a place is stepped at that place.
          scope.stepPosition('0.0', 1, at(0.01)),
        ]));
        """)
        assert out == ["1.2", "1.13", "0.03", "0.1"]

    def test_a_bare_zero_steps_a_tenth_of_a_division(self):
        """At 10 mV/div, 0 must not jump a volt: a hundred divisions."""
        out = _run_js("""
        process.stdout.write(JSON.stringify({
          tenMv: scope.scaleStep(0.01),
          oneV: scope.scaleStep(1),
          halfV: scope.scaleStep(0.5),
          up: scope.stepPosition('0', 1, scope.scaleStep(0.01)),
          down: scope.stepPosition('-0', -1, scope.scaleStep(0.01)),
          // A finer typed digit is stepped as written.
          fine: scope.stepPosition('0.0005', 1, scope.scaleStep(0.01)),
          // A coarse scale does not coarsen a typed value's step.
          coarse: scope.stepPosition('1.1', 1, scope.scaleStep(100)),
          // Nor a bare zero's past its own units digit.
          coarseZero: scope.stepPosition('0', 1, scope.scaleStep(100)),
        }));
        """)
        assert out["tenMv"] == pytest.approx(1e-3)
        assert out["oneV"] == pytest.approx(0.1)
        assert out["halfV"] == pytest.approx(0.01)
        assert out["up"] == "0.001"
        assert out["down"] == "-0.001"
        assert out["fine"] == "0.0006"
        assert out["coarse"] == "1.2"
        assert out["coarseZero"] == "1"

    def test_exponent_form_counts_its_places(self):
        assert _steps([["1e-3", 1, None], ["1.5e-3", 1, None]]) == ["0.002", "0.0016"]

    def test_more_places_than_tofixed_takes_still_steps(self):
        """"1e-101" asks for 101 places; toFixed throws past 100."""
        [up] = _steps([["1e-101", 1, None]])
        assert float(up) == pytest.approx(1e-100)


class _Field:
    """A fake input, unit selector and pair of buttons, driven through
    wirePositionField.

    Timers run on a fake clock: `runTimer()` fires the one due next and
    resolves to its delay, and `pending` in the result counts those left.
    `extra` is whatever the body leaves in `globalThis.extra`.
    """

    PRELUDE = """
    const make = (value) => {
      const handlers = {};
      return {
        value,
        focused: false,
        focus() { this.focused = true; },
        addEventListener(type, fn) { (handlers[type] ||= []).push(fn); },
        async fire(type, extra = {}) {
          const event = { preventDefault() {}, ...extra };
          for (const fn of handlers[type] || []) await fn(event);
        },
      };
    };
    const timers = new Map();
    let lastTimer = 0;
    globalThis.setTimeout = (fn, ms) => { timers.set(++lastTimer, { fn, ms }); return lastTimer; };
    globalThis.clearTimeout = (id) => { timers.delete(id); };
    const runTimer = async () => {
      const [id, { fn, ms }] = timers.entries().next().value;
      timers.delete(id);
      fn();
      await new Promise((resolve) => setImmediate(resolve));
      return ms;
    };
    const input = make('0');
    const unit = make('1');
    const up = make();
    const down = make();
    let held = 0;
    const applied = [];
    let LIMIT = 5e-3;
    const field = scope.wirePositionField(input, unit, {
      live: %s,
      perDiv: () => 1e-3,
      get: () => held,
      set: async (value, options) => {
        held = Math.max(-LIMIT, Math.min(LIMIT, value));
        applied.push([value, options.announce]);
        // The page's setters show what they applied, which writes the field
        // when it is not focused. Off by default: nothing here has focus.
        if (globalThis.echo) field.show(held);
        return held;
      },
      up,
      down,
    });
    const key = (k) => input.fire('keydown', { key: k });
    const type = async (text) => { input.value = text; await input.fire('input'); };
    // A pointer's whole press, as a browser sends it: down, up, then click.
    const MOUSE = { button: 0, pointerType: 'mouse' };
    const press = async (button, pointer = MOUSE) => {
      await button.fire('pointerdown', pointer);
      await button.fire('pointerup', pointer);
      await button.fire('click', { detail: 1 });
    };
    """

    @classmethod
    def run(cls, live, body):
        return _run_js(cls.PRELUDE % json.dumps(live) + body + """
        process.stdout.write(JSON.stringify({
          value: input.value, held, applied, focused: input.focused,
          pending: timers.size, extra: globalThis.extra ?? null }));
        """)


class TestTheHorizontalFieldWaitsForEnter:
    """Every applied value is a fresh capture, so typing sends nothing."""

    def test_typing_applies_nothing(self):
        out = _Field.run(False, "await type('0.002');")
        assert out["applied"] == []

    def test_enter_applies_it(self):
        out = _Field.run(False, "await type('0.002'); await key('Enter');")
        assert out["applied"] == [[0.002, True]]

    def test_leaving_the_field_applies_it(self):
        out = _Field.run(False, "await type('0.002'); await input.fire('blur');")
        assert out["applied"] == [[0.002, True]]

    def test_escape_puts_back_the_value_applied_last(self):
        out = _Field.run(False, """
        await type('0.002'); await key('Enter');
        await type('0.004'); await key('Escape'); await input.fire('blur');
        """)
        assert out["applied"] == [[0.002, True]]
        assert out["value"] == "0.002"

    def test_arrows_apply_at_once(self):
        out = _Field.run(False, "await key('ArrowUp');")
        # From 0 at 1 ms/div the step is capped at a tenth of a division.
        assert out["applied"] == [[pytest.approx(1e-4), True]]
        assert out["value"] == "0.0001"

    def test_a_clamped_value_is_written_back(self):
        out = _Field.run(False, "await type('1'); await key('Enter');")
        assert out["held"] == pytest.approx(5e-3)
        assert out["value"] == "0.005"

    def test_the_unit_rescales_what_is_shown_not_what_is_held(self):
        """0.1 s read in ms is 100."""
        out = _Field.run(False, """
        held = 0.1;
        unit.value = String(1e-3);
        await unit.fire('change');
        """)
        assert out["value"] == "100"
        assert out["held"] == 0.1
        assert out["applied"] == []

    def test_in_ms_a_typed_value_is_ms(self):
        out = _Field.run(False, """
        unit.value = String(1e-3);
        await type('2'); await key('Enter');
        """)
        assert out["applied"] == [[pytest.approx(2e-3), True]]

    # More than six significant figures: the field shows 0.00123457, which
    # is not the value held, so only an edit can say the field was changed.
    UNROUNDED = "held = 0.0012345678; field.show(held);"

    def test_leaving_it_untouched_applies_nothing(self):
        out = _Field.run(False, self.UNROUNDED + "await input.fire('blur');")
        assert out["applied"] == []
        assert out["value"] == "0.00123457"
        assert out["held"] == 0.0012345678

    def test_escape_then_leaving_applies_nothing(self):
        out = _Field.run(False, self.UNROUNDED + """
        await type('0.004'); await key('Escape'); await input.fire('blur');
        """)
        assert out["applied"] == []
        assert out["value"] == "0.00123457"
        assert out["held"] == 0.0012345678

    def test_leaving_after_enter_does_not_apply_it_twice(self):
        """A clamp writes back a rounded value, which is not an edit."""
        out = _Field.run(False, """
        await type('1'); await key('Enter'); await input.fire('blur');
        """)
        assert out["applied"] == [[1, True]]

    def test_an_edit_back_to_the_same_value_applies_nothing(self):
        out = _Field.run(False, """
        held = 0.002; field.show(held);
        await type('0.002'); await input.fire('blur');
        """)
        assert out["applied"] == []


class TestTheVerticalFieldIsLive:
    """Nothing is sent to the scope, so the trace follows each keystroke."""

    def test_typing_applies_as_it_goes_quietly(self):
        out = _Field.run(True, "await type('0.003');")
        assert out["applied"] == [[0.003, False]]

    def test_a_half_typed_minus_is_not_applied(self):
        out = _Field.run(True, "await type('-');")
        assert out["applied"] == []

    def test_leaving_the_field_reports_a_clamp(self):
        """Quiet while typing, so "50" is not two notices on the way."""
        out = _Field.run(True, "await type('1'); await input.fire('blur');")
        assert out["applied"][-1] == [1, True]
        assert out["value"] == "0.005"

    def test_leaving_it_untouched_does_not_round_the_position(self):
        """The shared blur path: 1.23457 must not replace 1.2345678."""
        out = _Field.run(True, """
        held = 0.0012345678; field.show(held); await input.fire('blur');
        """)
        assert out["applied"] == []
        assert out["held"] == 0.0012345678


class TestAValueAppliedElsewhere:
    """`show()` writes the field with what the page applied."""

    def test_a_field_that_says_it_already_keeps_its_places(self):
        out = _Field.run(False, "input.value = '1.20'; field.show(1.2);")
        assert out["value"] == "1.20"

    def test_a_different_value_is_written(self):
        out = _Field.run(False, "input.value = '1.20'; field.show(1.3);")
        assert out["value"] == "1.3"


class TestAHeldArrowKey:
    """Auto-repeat outruns the box: one set in flight, only the newest queued."""

    def test_only_the_first_and_the_newest_are_sent(self):
        out = _run_js("""
        const handlers = {};
        const input = {
          value: '0',
          addEventListener(type, fn) { (handlers[type] ||= []).push(fn); },
        };
        const unit = { value: '1', addEventListener() {} };
        let held = 0;
        const sent = [];
        const pending = [];
        scope.wirePositionField(input, unit, {
          live: false,
          perDiv: () => 1e-3,
          get: () => held,
          set: (value) => new Promise((resolve) => {
            sent.push(value);
            pending.push(() => { held = value; resolve(value); });
          }),
        });
        const press = () => handlers.keydown[0]({ key: 'ArrowUp', preventDefault() {} });
        const presses = [press(), press(), press(), press()];
        const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
        await tick();
        const inFlight = sent.length;
        while (pending.length) { pending.shift()(); await tick(); }
        await Promise.all(presses);
        process.stdout.write(JSON.stringify({ inFlight, sent, held, value: input.value }));
        """)
        assert out["inFlight"] == 1
        assert out["sent"] == [pytest.approx(1e-4), pytest.approx(4e-4)]
        assert out["held"] == pytest.approx(4e-4)
        assert out["value"] == "0.0004"


class TestTheArrowButtons:
    """The up and down buttons inside each field: the arrow keys' step, by pointer."""

    def test_they_step_the_last_digit_shown(self):
        """1.14 down is 1.13, 1.1 up is 1.2, and 1.19 up is 1.20."""
        out = _Field.run(False, """
        unit.value = String(1e-3);
        globalThis.extra = [];
        for (const [text, button] of [['1.14', down], ['1.1', up], ['1.19', up]]) {
          await type(text);
          await press(button);
          globalThis.extra.push(input.value);
        }
        """)
        assert out["extra"] == ["1.13", "1.2", "1.20"]
        assert out["applied"] == [[pytest.approx(v), True] for v in (1.13e-3, 1.2e-3, 1.2e-3)]

    def test_a_mouse_press_steps_once_and_focuses_the_field(self):
        """Focused, as a number field is by its spinner, so the box's readback
        leaves the field alone between presses. The press's own click is not
        a second step."""
        out = _Field.run(False, "await press(up);")
        assert out["applied"] == [[pytest.approx(1e-4), True]]
        assert out["value"] == "0.0001"
        assert out["focused"] is True
        assert out["pending"] == 0

    def test_a_touch_press_leaves_the_focus_alone(self):
        """Focusing the field on a touch screen opens the on-screen keyboard."""
        out = _Field.run(False, "await press(up, { button: 0, pointerType: 'touch' });")
        assert len(out["applied"]) == 1
        assert out["focused"] is False

    def test_a_touch_press_keeps_the_places_through_a_carry(self):
        """Not focused, the field is written by the page's echo of what it
        applied. That echo is 1.2 for 1.20, which must not cost the next
        press its hundredth."""
        out = _Field.run(False, """
        globalThis.echo = true;
        unit.value = String(1e-3);
        const TOUCH = { button: 0, pointerType: 'touch' };
        await type('1.19');
        globalThis.extra = [];
        for (let i = 0; i < 2; i += 1) {
          await press(up, TOUCH);
          globalThis.extra.push(input.value);
        }
        """)
        assert out["extra"] == ["1.20", "1.21"]

    def test_a_click_with_no_pointer_is_one_step(self):
        """A screen reader activates a button with a click and nothing before it."""
        out = _Field.run(False, "await down.fire('click', { detail: 0 });")
        assert out["applied"] == [[pytest.approx(-1e-4), True]]
        assert out["value"] == "-0.0001"

    def test_a_press_from_a_script_is_still_one_step(self):
        """A click sent by a script, as a browser automation tool sends one,
        has a `detail` of 0 even at the end of a press."""
        out = _Field.run(False, """
        await up.fire('pointerdown', MOUSE);
        await up.fire('pointerup', MOUSE);
        await up.fire('click', { detail: 0 });
        """)
        assert out["applied"] == [[pytest.approx(1e-4), True]]

    def test_a_press_spends_only_its_own_click(self):
        """A screen reader's click after a press is still a step."""
        out = _Field.run(False, "await press(up); await up.fire('click', { detail: 0 });")
        assert [value for value, _ in out["applied"]] == [
            pytest.approx(1e-4), pytest.approx(2e-4)]

    def test_held_it_repeats_after_a_pause_until_released(self):
        out = _Field.run(False, """
        await up.fire('pointerdown', MOUSE);
        globalThis.extra = [await runTimer(), await runTimer(), await runTimer()];
        await up.fire('pointerup', MOUSE);
        """)
        pause, *rate = out["extra"]
        assert pause > rate[0] > 0 and rate[0] == rate[1]
        assert [value for value, _ in out["applied"]] == [
            pytest.approx(n * 1e-4) for n in (1, 2, 3, 4)]
        assert out["value"] == "0.0004"
        assert out["pending"] == 0

    @pytest.mark.parametrize("end", ["pointerup", "pointerleave", "pointercancel"])
    def test_a_press_ends_with_the_pointer(self, end):
        out = _Field.run(False, """
        await up.fire('pointerdown', MOUSE);
        await up.fire(%s, MOUSE);
        """ % json.dumps(end))
        assert len(out["applied"]) == 1
        assert out["pending"] == 0

    def test_a_disabled_button_does_nothing(self):
        out = _Field.run(False, "up.disabled = true; await press(up);")
        assert out["applied"] == [] and out["pending"] == 0

    def test_only_the_primary_button_presses(self):
        """A browser sends no click for the other buttons, only the press."""
        out = _Field.run(False, """
        const RIGHT = { button: 2, pointerType: 'mouse' };
        await up.fire('pointerdown', RIGHT);
        await up.fire('pointerup', RIGHT);
        """)
        assert out["applied"] == [] and out["pending"] == 0

    def test_a_typed_value_is_stepped_from_not_applied_first(self):
        """The horizontal field holds a typed value until Enter. A press steps
        from it and applies once: one fresh capture, not two, and leaving the
        field afterwards applies nothing more."""
        out = _Field.run(False, """
        await type('0.002'); await press(up); await input.fire('blur');
        """)
        assert out["applied"] == [[pytest.approx(0.003), True]]
        assert out["value"] == "0.003"

    def test_a_clamp_reached_by_a_step_is_shown_in_full(self):
        """From "5" ms against a 5.12 ms limit: 5.12, not 5."""
        out = _Field.run(False, """
        LIMIT = 5.12e-3;
        unit.value = String(1e-3);
        held = 5e-3; field.show(held);
        await press(up);
        """)
        assert out["held"] == pytest.approx(5.12e-3)
        assert out["value"] == "5.12"


class _Tree(html.parser.HTMLParser):
    """Each element with an id: its tag, attributes and ancestors."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.by_id = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.by_id[attrs["id"]] = {
                "tag": tag, "attrs": attrs, "parents": list(self.stack)}
        if tag not in self.VOID:
            self.stack.append((tag, attrs.get("class", "")))

    def handle_endtag(self, tag):
        while tag not in self.VOID and self.stack and self.stack.pop()[0] != tag:
            pass


class TestTheButtonsAreOnEveryPositionField:

    def test_a_channel_field_gets_up_then_down_inside_it(self):
        out = _run_js("""
        const make = (tag) => ({
          tag, children: [], attrs: {},
          append(...kids) { this.children.push(...kids); },
          setAttribute(name, value) { this.attrs[name] = value; },
        });
        globalThis.document = { createElement: make };
        const input = make('input');
        const { wrap, up, down } = scope.stepperFor(input, 'channel B vertical position');
        const [field, buttons] = wrap.children;
        process.stdout.write(JSON.stringify({
          wrap: [wrap.tag, wrap.className],
          field: field === input,
          buttons: [buttons.tag, buttons.className],
          wired: buttons.children[0] === up && buttons.children[1] === down,
          each: [up, down].map((b) => [b.tag, b.type, b.className, b.tabIndex,
                                     b.attrs['aria-label']]),
        }));
        """)
        assert out == {
            "wrap": ["span", "stepper"],
            "field": True,
            "buttons": ["span", "stepper__buttons"],
            "wired": True,
            "each": [
                ["button", "button", "stepper__button", -1,
                 "Increase channel B vertical position"],
                ["button", "button", "stepper__button", -1,
                 "Decrease channel B vertical position"],
            ],
        }

    def test_the_horizontal_field_has_the_same_two(self):
        """Written in the page rather than built, so held to the same shape."""
        text = INDEX_HTML.read_text()
        tree = _Tree()
        tree.feed(text)
        assert tree.by_id["time-position"]["parents"][-1] == ("span", "stepper")
        for name, verb in (("time-position-up", "Increase"),
                           ("time-position-down", "Decrease")):
            button = tree.by_id[name]
            assert button["tag"] == "button"
            assert button["parents"][-2:] == [("span", "stepper"),
                                              ("span", "stepper__buttons")]
            assert button["attrs"]["class"] == "stepper__button"
            assert button["attrs"]["type"] == "button"
            assert button["attrs"]["tabindex"] == "-1"
            assert button["attrs"]["aria-label"] == verb + " horizontal position"
        assert text.index('id="time-position-up"') < text.index('id="time-position-down"')
        js = SCOPE_JS.read_text()
        assert "up: el('time-position-up')" in js
        assert "down: el('time-position-down')" in js

    @staticmethod
    def _buttons_of_strip(net):
        """Each stepper button's disabled flag, from a channel B strip."""
        return _run_js("""
        const make = () => ({
          children: [], style: {}, classList: { add() {} },
          append(...kids) { this.children.push(...kids); },
          appendChild(kid) { this.children.push(kid); },
          replaceChildren(...kids) { this.children = kids; },
          addEventListener() {}, setAttribute() {},
        });
        globalThis.document = {
          documentElement: {}, createElement: make,
          createTextNode: (text) => ({ text }), getElementById: () => null,
        };
        const self = Object.assign(Object.create(scope.ScopeApp.prototype), {
          channelState: new Map([['B', { enabled: false, net: %s, attenuation: 1 }]]),
        });
        const found = [];
        const walk = (node) => {
          if (node.className === 'stepper__button') found.push(node.disabled === true);
          for (const kid of node.children || []) walk(kid);
        };
        walk(self.buildChannelStrip('B', 1, {}));
        process.stdout.write(JSON.stringify(found));
        """ % json.dumps(net))

    def test_a_wired_channel_has_them(self):
        assert self._buttons_of_strip("scope2") == [False, False]

    def test_an_unwired_channel_has_them_disabled(self):
        """With the field, which has no trace to move."""
        assert self._buttons_of_strip(None) == [True, True]


def _execute(line, capabilities=None, state_reply=None, timebase=None):
    """Run one console line through ScopeApp.execute with two wired channels.

    `timebase` is the s/div the timebase control shows, for `hpos`.
    """
    return _run_js("""
    const timebase = %s;
    if (timebase !== null) {
      globalThis.document = {
        activeElement: null,
        getElementById: (id) => (id === 'timebase' ? { value: String(timebase) } : null),
      };
    }
    const requests = [];
    globalThis.fetch = async (url, init) => {
      const body = JSON.parse(init.body);
      requests.push(body);
      return { ok: true, status: 200,
               json: async () => (%s || { success: true, message: `${body.action}: ok` }) };
    };
    const lines = [];
    const self = Object.assign(Object.create(scope.ScopeApp.prototype), {
      net: 'pico1',
      capabilities: %s,
      channelNets: [{ name: 'scope1', pin: 1 }, { name: 'scope2', pin: 2 }],
      channelState: new Map([
        ['A', { enabled: true, net: 'scope1', voltsPerDiv: 1, positionV: 0 }],
        ['B', { enabled: false, net: 'scope2', voltsPerDiv: 1, positionV: 0 }],
      ]),
      requestRedraw() {},
      console: {
        write: (text, kind = 'ok') => lines.push([kind, text]),
        error: (text) => lines.push(['error', text]),
        note: (text) => lines.push(['note', text]),
      },
    });
    await self.execute(%s);
    process.stdout.write(JSON.stringify({
      requests, lines,
      positions: Object.fromEntries([...self.channelState].map(([l, s]) => [l, s.positionV])),
      timePosition: self.timePositionS ?? null,
    }));
    """ % (json.dumps(timebase), json.dumps(state_reply), json.dumps(capabilities),
           json.dumps(line)))


class TestVposIsDisplayOnly:

    def test_it_moves_the_named_trace_and_sends_nothing(self):
        out = _execute("vpos B -0.5")
        assert out["requests"] == []
        assert out["positions"] == {"A": 0, "B": -0.5}
        assert "display only" in out["lines"][-1][1]

    def test_millivolts_are_understood(self):
        assert _execute("vpos B 200mV")["positions"]["B"] == pytest.approx(0.2)

    def test_with_no_channel_it_is_the_first_one_on(self):
        assert _execute("vpos 0.25")["positions"] == {"A": 0.25, "B": 0}

    def test_a_channel_the_scope_lacks_is_refused(self):
        out = _execute("vpos C 1")
        assert out["lines"] == [["error", "this scope has no channel C"]]

    def test_status_reports_each_channels_position(self):
        out = _execute("status", state_reply={"success": True, "message": "state"})
        assert [r["action"] for r in out["requests"]] == ["get_state"]
        texts = [text for _, text in out["lines"]]
        assert any(t.startswith("channel A: vertical position") for t in texts)
        assert any(t.startswith("channel B: vertical position") for t in texts)


class TestHposIsHeldToTheScreen:
    """The console goes through the same clamp and note as the sidebar field."""

    PER_DIV = 1.02e-3

    def test_a_value_past_the_edge_is_clamped_with_a_note(self):
        out = _execute("hpos 100ms", timebase=self.PER_DIV)
        [request] = out["requests"]
        assert request["action"] == "set_time_offset"
        assert request["params"]["offset"] == pytest.approx(5 * self.PER_DIV)
        assert out["timePosition"] == pytest.approx(5 * self.PER_DIV)
        notes = [text for kind, text in out["lines"] if kind == "note"]
        assert len(notes) == 1 and "Horizontal position limited to" in notes[0]

    def test_looking_back_is_clamped_the_same_way(self):
        out = _execute("hpos -1", timebase=self.PER_DIV)
        assert out["requests"][0]["params"]["offset"] == pytest.approx(-5 * self.PER_DIV)

    def test_a_value_on_screen_is_sent_as_typed_without_a_note(self):
        out = _execute("hpos 2ms", timebase=self.PER_DIV)
        assert out["requests"][0]["params"]["offset"] == pytest.approx(2e-3)
        assert [kind for kind, _ in out["lines"]] == ["ok"]

    def test_a_read_is_not_clamped_or_noted(self):
        out = _execute("hpos", timebase=self.PER_DIV)
        assert [r["action"] for r in out["requests"]] == ["get_time_offset"]
        assert out["requests"][0]["params"] == {}
        assert all(kind != "note" for kind, _ in out["lines"])
        assert out["timePosition"] is None


class TestOffsetIsGatedOnTheCapability:
    """A legacy ps2000 has no analog offset; a modern PicoScope does."""

    LEGACY = {"model": "2204A", "analog_offset": False}
    MODERN = {"model": "5444D", "analog_offset": True}

    def test_a_scope_without_it_refuses_up_front_and_names_vpos(self):
        out = _execute("offset 0.5", capabilities=self.LEGACY)
        assert out["requests"] == []
        [(kind, text)] = out["lines"]
        assert kind == "error"
        assert "no analog offset" in text and "vpos" in text and "Position" in text

    def test_reading_it_or_clearing_it_still_reaches_the_box(self):
        """Zero is what the daemon accepts, and a read has nothing to refuse."""
        for line in ("offset", "offset 0"):
            out = _execute(line, capabilities=self.LEGACY)
            assert len(out["requests"]) == 1, line

    def test_a_scope_with_it_sends_it(self):
        out = _execute("offset 0.5", capabilities=self.MODERN)
        assert out["requests"] == [{
            "netname": "scope1", "action": "set_offset", "params": {"offset": 0.5}}]

    def test_unknown_capabilities_do_not_block_it(self):
        """A box that reports none, or a Rigol: the daemon decides."""
        out = _execute("offset 0.5", capabilities=None)
        assert len(out["requests"]) == 1
