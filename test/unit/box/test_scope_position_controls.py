# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
The scope page's position controls: how the arrow keys step them, when a
typed value takes effect, and what the console says about them.

The two positions differ on purpose. Vertical never leaves the page, so it is
applied as it is typed; horizontal re-arms the scope for a fresh capture, so
a typed value waits for Enter. The arrow keys apply at once on both.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCOPE_JS = REPO_ROOT / "box" / "lager" / "static" / "scope" / "scope.js"

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
    """The step is the last digit written, never coarser than the scale."""

    def test_the_last_digit_is_the_one_stepped(self):
        assert _steps([["1.15", 1, None], ["1.1", 1, None], ["1.1", -1, None]]) == [
            "1.16", "1.2", "1.0"]

    def test_a_carry_keeps_the_places(self):
        """1.20, not 1.2: the next press must still step by 0.01."""
        assert _steps([["1.19", 1, None], ["0.99", 1, None], ["-0.01", 1, None]]) == [
            "1.20", "1.00", "0.00"]

    def test_no_float_noise(self):
        assert _steps([["0.29", 1, None], ["0.7", -1, None]]) == ["0.30", "0.6"]

    def test_the_scale_caps_the_step(self):
        """At 10 mV/div, 0 must not jump a volt: a hundred divisions."""
        out = _run_js("""
        process.stdout.write(JSON.stringify({
          tenMv: scope.scaleStep(0.01),
          oneV: scope.scaleStep(1),
          halfV: scope.scaleStep(0.5),
          up: scope.stepPosition('0', 1, scope.scaleStep(0.01)),
          // A finer typed digit still wins over the cap.
          fine: scope.stepPosition('0.0005', 1, scope.scaleStep(0.01)),
          // A coarse scale does not coarsen a typed value's step.
          coarse: scope.stepPosition('1.1', 1, scope.scaleStep(100)),
        }));
        """)
        assert out["tenMv"] == pytest.approx(1e-3)
        assert out["oneV"] == pytest.approx(0.1)
        assert out["halfV"] == pytest.approx(0.01)
        assert out["up"] == "0.001"
        assert out["fine"] == "0.0006"
        assert out["coarse"] == "1.2"

    def test_exponent_form_counts_its_places(self):
        assert _steps([["1e-3", 1, None], ["1.5e-3", 1, None]]) == ["0.002", "0.0016"]


class _Field:
    """A fake input and unit selector, driven through wirePositionField."""

    PRELUDE = """
    const make = (value) => {
      const handlers = {};
      return {
        value,
        addEventListener(type, fn) { (handlers[type] ||= []).push(fn); },
        async fire(type, extra = {}) {
          const event = { preventDefault() {}, ...extra };
          for (const fn of handlers[type] || []) await fn(event);
        },
      };
    };
    const input = make('0');
    const unit = make('1');
    let held = 0;
    const applied = [];
    const LIMIT = 5e-3;
    const field = scope.wirePositionField(input, unit, {
      live: %s,
      perDiv: () => 1e-3,
      get: () => held,
      set: async (value, options) => {
        held = Math.max(-LIMIT, Math.min(LIMIT, value));
        applied.push([value, options.announce]);
        return held;
      },
    });
    const key = (k) => input.fire('keydown', { key: k });
    const type = async (text) => { input.value = text; await input.fire('input'); };
    """

    @classmethod
    def run(cls, live, body):
        return _run_js(cls.PRELUDE % json.dumps(live) + body + """
        process.stdout.write(JSON.stringify({ value: input.value, held, applied }));
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


def _execute(line, capabilities=None, state_reply=None):
    """Run one console line through ScopeApp.execute with two wired channels."""
    return _run_js("""
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
    }));
    """ % (json.dumps(state_reply), json.dumps(capabilities), json.dumps(line)))


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
