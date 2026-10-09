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
import math
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


# A box with one net per channel, as `lager nets add` leaves it, in the shape
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


class TestTheSidebarCollapses:
    """Hiding the controls gives the plot that column. On a narrow window the
    panel stacks under the plot, so hiding it gives the plot that row too."""

    def test_a_hidden_sidebar_drops_its_column(self):
        css = SCOPE_CSS.read_text()
        rule = re.search(r"html\.is-sidebar-hidden \.layout \{[^}]*\}", css)
        assert rule, "hiding the sidebar leaves its 280px column beside the plot"
        assert "grid-template-columns" in rule.group(0)
        assert "280px" not in rule.group(0)
        hidden = re.search(r"html\.is-sidebar-hidden \.controls \{[^}]*\}", css)
        assert hidden and "display: none" in hidden.group(0)
        # The narrow layout later sets display:grid on .controls. A plain
        # display:none loses to that, and the panel comes back under the plot.
        assert "!important" in hidden.group(0)

    def test_a_narrow_window_drops_the_stacked_row(self):
        css = SCOPE_CSS.read_text()
        narrow = css.split("@media (max-width: 820px)", 1)[1]
        assert "grid-template-rows: minmax(0, 1fr)" in narrow
        assert "--plot-reserve: 160px" in narrow

    def test_the_page_remembers_with_the_key_the_script_reads(self):
        html = (SCOPE_DIR / "index.html").read_text()
        js = SCOPE_JS.read_text()
        for name in ("lager-scope-sidebar", "is-sidebar-hidden"):
            assert name in html and name in js, name
        assert 'id="btn-sidebar"' in html
        assert 'id="scope-controls"' in html


@needs_node
class TestSidebarCommandHidesItAndSendsNothing:
    """`sidebar` is page chrome. Sending it would ask the box to run a command
    it does not have."""

    HARNESS = """
    function classList(initial) {
      const names = new Set(initial || []);
      return {
        add(name) { names.add(name); },
        contains(name) { return names.has(name); },
        toggle(name, force) {
          const on = force === undefined ? !names.has(name) : Boolean(force);
          if (on) names.add(name); else names.delete(name);
          return on;
        },
      };
    }
    function install(hidden) {
      const button = {
        attrs: {}, textContent: 'Hide sidebar', title: '', focused: false,
        setAttribute(key, value) { this.attrs[key] = value; },
        focus() { this.focused = true; },
      };
      const inside = { id: 'level' };
      const aside = { contains(node) { return node === inside; } };
      const root = { classList: classList(hidden ? ['is-sidebar-hidden'] : []) };
      const store = {};
      globalThis.document = {
        documentElement: root,
        activeElement: inside,
        getElementById(id) {
          if (id === 'btn-sidebar') return button;
          if (id === 'scope-controls') return aside;
          return null;
        },
      };
      globalThis.localStorage = {
        getItem(key) {
          return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
        },
        setItem(key, value) { store[key] = String(value); },
      };
      const lines = [];
      const sent = [];
      const self = Object.create(ScopeApp.prototype);
      self.console = {
        write(text) { lines.push(text); },
        error(text) { lines.push('ERR ' + text); },
      };
      self.runCommand = async (...args) => { sent.push(args); return {}; };
      return { self, button, root, store, lines, sent };
    }
    """

    def run(self, *lines, hidden=False):
        return _run_js(self.HARNESS + """
        const world = install(%s);
        for (const line of %s) {
          await ScopeApp.prototype.execute.call(world.self, line);
        }
        process.stdout.write(JSON.stringify({
          lines: world.lines, sent: world.sent,
          hidden: world.root.classList.contains('is-sidebar-hidden'),
          text: world.button.textContent,
          expanded: world.button.attrs['aria-expanded'] || null,
          focused: world.button.focused,
          store: world.store,
        }));
        """ % ("true" if hidden else "false", json.dumps(list(lines))))

    def test_sidebar_off_hides_it_and_sends_nothing(self):
        out = self.run("sidebar off")
        assert out["sent"] == []
        assert out["hidden"] is True
        assert out["lines"] == ["sidebar hidden"]
        assert out["text"] == "Show sidebar"
        assert out["expanded"] == "false"
        assert out["focused"] is True
        assert out["store"] == {"lager-scope-sidebar": "hidden"}

    def test_sidebar_on_shows_it_again(self):
        out = self.run("sidebar hide", "sidebar show", hidden=True)
        assert out["sent"] == []
        assert out["hidden"] is False
        assert out["lines"] == ["sidebar hidden", "sidebar shown"]
        assert out["text"] == "Hide sidebar"
        assert out["expanded"] == "true"
        assert out["store"] == {"lager-scope-sidebar": "shown"}

    def test_sidebar_alone_reports_and_changes_nothing(self):
        out = self.run("sidebar", hidden=True)
        assert out["sent"] == []
        assert out["hidden"] is True
        assert out["lines"] == ["sidebar hidden"]
        assert out["text"] == "Hide sidebar"
        assert out["store"] == {}

    def test_a_bad_sidebar_word_is_an_error_and_sends_nothing(self):
        out = self.run("sidebar left", "sidebar off now")
        assert out["sent"] == []
        assert out["hidden"] is False
        assert out["store"] == {}
        assert len(out["lines"]) == 2
        assert all(line.startswith("ERR ") and "sidebar takes" in line
                   for line in out["lines"])

    def test_the_button_hides_it_without_a_console_line(self):
        out = _run_js(self.HARNESS + """
        const world = install(false);
        ScopeApp.prototype.setSidebar.call(world.self, false);
        process.stdout.write(JSON.stringify({
          lines: world.lines,
          hidden: world.root.classList.contains('is-sidebar-hidden'),
          text: world.button.textContent,
          store: world.store,
        }));
        """)
        assert out == {
            "lines": [], "hidden": True, "text": "Show sidebar",
            "store": {"lager-scope-sidebar": "hidden"},
        }


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

    @pytest.mark.parametrize("pin_a, pin_b", [
        ("A", "B"), ("a", "b"), ("CH1", "CH2"), ("ch1", "ch2"),
        ("CHAN1", "CHANNEL2"), ("1", "2"), (1, 2),
    ])
    def test_a_pin_is_read_the_way_the_box_reads_it(self, pin_a, pin_b):
        """The box drives channel B for pin "B" or "CH2" as well as for 2.

        Only numbers were understood here, so those pins read as missing and
        the channels went by list order: with B's net listed first, channel
        A's strip drove channel B.
        """
        nets = [{"name": "scope2", "pin": pin_b}, {"name": "scope1", "pin": pin_a}]
        out = _run_js("""
        const f = ScopeApp.prototype.netForChannel;
        const self = { scopeNets: %s };
        process.stdout.write(JSON.stringify({
          a: f.call(self, 0), b: f.call(self, 1), c: f.call(self, 2),
        }));
        """ % json.dumps(nets))
        assert out == {"a": "scope1", "b": "scope2", "c": None}

    def test_a_pin_that_names_no_channel_is_no_pin(self):
        out = _run_js("""
        const { pinIndex } = await import(%s);
        process.stdout.write(JSON.stringify(
          [0, '0', '', null, undefined, 'CH', 'AB', 'CH-1', 'X1']
            .map((pin) => pinIndex({ pin }))
            .concat([pinIndex({}), pinIndex(null), pinIndex({ pin: 'D' })])));
        """ % json.dumps(str(SCOPE_JS)))
        # Zero is what the box saves for a net with no pin at all.
        assert out == [None] * 11 + [3]


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
class TestEnableNamesAChannel:
    """`enable B` must reach channel B's net, not the first channel's."""

    def test_enable_b_is_sent_to_scope2_and_checks_its_box(self):
        out = _run_js("""
        const sent = [];
        const toggles = { A: { checked: false }, B: { checked: false } };
        const self = {
          net: 'picoscope1',
          channelNets: [
            { name: 'scope1', pin: 1, role: 'scope-channel' },
            { name: 'scope2', pin: 2, role: 'scope-channel' },
          ],
          channelState: new Map([
            ['A', { enabled: false, net: 'scope1', toggle: toggles.A }],
            ['B', { enabled: false, net: 'scope2', toggle: toggles.B }],
          ]),
          console: { error(text) { this.message = text; } },
          netForLabel: ScopeApp.prototype.netForLabel,
          netForChannel: ScopeApp.prototype.netForChannel,
          runCommand: async (action, params, summary, net) => {
            sent.push([action, net]);
            return { message: 'ok' };
          },
          refreshMeasurements() {},
        };
        await ScopeApp.prototype.execute.call(self, 'enable B');
        await ScopeApp.prototype.execute.call(self, 'enable A');
        process.stdout.write(JSON.stringify({
          sent, a: toggles.A.checked, b: toggles.B.checked,
        }));
        """)
        assert out["sent"] == [
            ["enable_net", "scope2"],
            ["enable_net", "scope1"],
        ]
        assert out["b"] is True
        assert out["a"] is True

    def test_enable_with_no_letter_still_uses_the_first_channel(self):
        out = _run_js("""
        const sent = [];
        const self = {
          net: 'picoscope1',
          channelNets: [{ name: 'scope1', pin: 1 }, { name: 'scope2', pin: 2 }],
          channelState: new Map(),
          console: { error(text) { this.message = text; } },
          netForLabel: ScopeApp.prototype.netForLabel,
          netForChannel: ScopeApp.prototype.netForChannel,
          netForAction: ScopeApp.prototype.netForAction,
          runCommand: async (action, params, summary, net) => {
            sent.push(self.netForAction(action, net));
            return { message: 'ok' };
          },
          refreshMeasurements() {},
        };
        await ScopeApp.prototype.execute.call(self, 'enable');
        process.stdout.write(JSON.stringify(sent));
        """)
        assert out == ["scope1"]


@needs_node
class TestConsoleChannelCommandsFollowThePanel:
    """A per-channel console command with no channel reaches the panel's.

    The panel reads the first channel that is on. The console used to send
    every per-channel command with no channel named to the lowest channel
    instead. With A off and B on, `measure vpp` was refused (A has nothing to
    measure) and `scale 0.5` changed A -- which the box accepts without
    complaint, so a setting landed on a channel nobody was looking at.
    """

    # The real execute(), netForLabel() and netForAction(), with send()
    # replaced by a recorder of the net each request would reach.
    HARNESS = """
    function app(enabled) {
      const sent = [];
      const self = {
        net: 'picoscope1',
        channelNets: [
          { name: 'scope1', pin: 1, role: 'scope-channel' },
          { name: 'scope2', pin: 2, role: 'scope-channel' },
        ],
        channelState: new Map([
          ['A', { enabled: enabled.A, net: 'scope1' }],
          ['B', { enabled: enabled.B, net: 'scope2' }],
        ]),
        console: { error(text) { self.error = text; } },
        netForLabel: ScopeApp.prototype.netForLabel,
        netForChannel: ScopeApp.prototype.netForChannel,
        netForAction: ScopeApp.prototype.netForAction,
        measuredChannel: ScopeApp.prototype.measuredChannel,
        runCommand: async (action, params, summary, net) => {
          sent.push([action, self.netForAction(action, net)]);
          return { value: 1 };
        },
        refreshMeasurements() {},
      };
      self.sent = sent;
      return self;
    }
    async function run(enabled, lines) {
      const self = app(enabled);
      for (const line of lines) {
        await ScopeApp.prototype.execute.call(self, line);
      }
      return {
        sent: self.sent, error: self.error || null,
        panel: self.measuredChannel(),
      };
    }
    """

    def run(self, enabled, *lines):
        return _run_js(self.HARNESS + """
        const out = await run(%s, %s);
        process.stdout.write(JSON.stringify(out));
        """ % (json.dumps(enabled), json.dumps(list(lines))))

    def test_with_a_off_measure_reads_b_as_the_panel_does(self):
        out = self.run({"A": False, "B": True}, "measure vpp", "measure all")
        assert out["panel"] == {"label": "B", "net": "scope2"}
        assert out["sent"] == [["measure_vpp", "scope2"],
                               ["measure_all", "scope2"]]

    def test_with_both_on_measure_reads_a(self):
        out = self.run({"A": True, "B": True}, "measure vpp")
        assert out["sent"] == [["measure_vpp", "scope1"]]

    def test_a_named_channel_is_measured_whatever_is_on(self):
        """The box, not the page, says whether a channel that is off can be
        measured, so the request goes out and its answer is shown."""
        out = self.run({"A": False, "B": True},
                       "measure A vpp", "measure 2 freq", "measure B all")
        assert out["sent"] == [["measure_vpp", "scope1"],
                               ["measure_freq", "scope2"],
                               ["measure_all", "scope2"]]

    def test_with_no_channel_on_measure_says_so_and_sends_nothing(self):
        out = self.run({"A": False, "B": False}, "measure vpp")
        assert out["sent"] == []
        assert out["error"] == "No channel is on. Switch one on to measure."

    def test_a_named_channel_is_still_sent_with_none_on(self):
        out = self.run({"A": False, "B": False}, "measure B vpp")
        assert out["sent"] == [["measure_vpp", "scope2"]]
        assert out["error"] is None

    def test_with_a_off_every_channel_setting_reaches_b(self):
        out = self.run({"A": False, "B": True},
                       "scale 0.5", "coupling ac", "probe 10", "offset 0.1",
                       "scale", "spectrum")
        assert out["sent"] == [["set_scale", "scope2"],
                               ["set_coupling", "scope2"],
                               ["set_probe", "scope2"],
                               ["set_offset", "scope2"],
                               ["get_scale", "scope2"],
                               ["fft", "scope2"]]

    def test_a_named_channel_setting_reaches_that_channel_even_when_off(self):
        out = self.run({"A": False, "B": True},
                       "scale A 0.5", "coupling A dc", "probe A 10", "offset A 0")
        assert [net for _, net in out["sent"]] == ["scope1"] * 4

    def test_enable_with_no_channel_still_reaches_the_lowest(self):
        """Switching on is the exception: following the channel that is on
        would send a bare `enable` to a channel that already is."""
        out = self.run({"A": False, "B": True}, "enable", "disable")
        assert out["sent"] == [["enable_net", "scope1"],
                               ["disable_net", "scope1"]]

    def test_with_no_channel_on_a_setting_reaches_the_lowest(self):
        """A channel that is off can still be set up before it is switched
        on, and the box applies it; only a measurement has nothing to read."""
        out = self.run({"A": False, "B": False}, "scale 0.5")
        assert out["sent"] == [["set_scale", "scope1"]]
        assert out["error"] is None

    def test_cursor_readings_stay_on_the_scope_net(self):
        out = self.run({"A": False, "B": True}, "cursor")
        assert out["sent"] == [["measure_cursor", "picoscope1"]]

    def test_before_the_channels_are_known_measure_falls_back_to_the_lowest(self):
        """With no strips yet, "none is on" would be a guess, not a fact."""
        out = _run_js("""
        const self = {
          net: 'picoscope1',
          channelNets: [{ name: 'scope1', pin: 1 }, { name: 'scope2', pin: 2 }],
        };
        process.stdout.write(JSON.stringify([
          ScopeApp.prototype.netForAction.call(self, 'measure_vpp'),
          ScopeApp.prototype.netForAction.call(
            Object.assign({ channelState: new Map() }, self), 'measure_vpp'),
        ]));
        """)
        assert out == ["scope1", "scope1"]


@needs_node
class TestTheReplyNamesTheChannel:
    """`scale 0.5: ok` said nothing about which channel changed, which is how
    a write to the wrong one went unnoticed."""

    def reply(self, line, enabled, message="Vertical scale 0.5 V/div"):
        return _run_js("""
        const written = [];
        const self = {
          net: 'picoscope1',
          channelNets: [{ name: 'scope1', pin: 1 }, { name: 'scope2', pin: 2 }],
          channelState: new Map([
            ['A', { enabled: %s, net: 'scope1' }],
            ['B', { enabled: %s, net: 'scope2' }],
          ]),
          console: {
            write(text) { written.push(text); },
            error(text) { written.push('error: ' + text); },
          },
          netForLabel: ScopeApp.prototype.netForLabel,
          netForChannel: ScopeApp.prototype.netForChannel,
          netForAction: ScopeApp.prototype.netForAction,
          runCommand: ScopeApp.prototype.runCommand,
          send: async () => (%s),
          refreshMeasurements() {},
          adoptCursors() {},
        };
        await ScopeApp.prototype.execute.call(self, %s);
        process.stdout.write(JSON.stringify(written));
        """ % (json.dumps(enabled["A"]), json.dumps(enabled["B"]),
               json.dumps({"message": message} if message else {}),
               json.dumps(line)))

    def test_a_defaulted_setting_names_the_channel_it_changed(self):
        assert self.reply("scale 0.5", {"A": False, "B": True}) == [
            "channel B: Vertical scale 0.5 V/div"]

    def test_a_named_setting_names_its_channel(self):
        assert self.reply("scale A 0.5", {"A": False, "B": True}) == [
            "channel A: Vertical scale 0.5 V/div"]

    def test_a_reply_with_no_message_still_names_the_channel(self):
        assert self.reply("probe 10", {"A": True, "B": False}, message=None) == [
            "channel A: probe 10: ok"]

    def test_a_device_wide_command_names_no_channel(self):
        assert self.reply("timebase 1e-3", {"A": True, "B": True},
                          message="Timebase 1 ms/div") == ["Timebase 1 ms/div"]


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

    def test_every_step_reads_as_a_plain_number(self):
        """At two figures, toPrecision made 100 mV "1.0e+2 mV"."""
        labels = _run_js("""
        const { si } = await import(%s);
        process.stdout.write(JSON.stringify(
          [0.01, 0.1, 0.2, 0.5, 1, 20, 200, 2000].map((v) => si(v, 'V', 2))));
        """ % json.dumps(str(SCOPE_JS)))
        assert labels == ["10 mV", "100 mV", "200 mV", "500 mV",
                          "1.0 V", "20 V", "200 V", "2.0 kV"]


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
class TestADisabledChannelLeavesThePlot:
    """Turning a channel off stops its samples. The frame already on screen
    still holds them, and drawing that frame left a trace that had frozen."""

    HARNESS = """
    function stand() {
      const self = Object.create(ScopeApp.prototype);
      self.channelState = new Map([
        ['A', { enabled: true, voltsPerDiv: 1, positionV: 0, net: 'scope1',
                toggle: { checked: true } }],
        ['B', { enabled: true, voltsPerDiv: 1, positionV: 0, net: 'scope2',
                toggle: { checked: true } }],
      ]);
      self.display = {};
      self.showTriggerMarkers = false;
      self.cursors = null;
      self.persist = { kept: true };
      self.dirty = false;
      self.drawn = [];
      self.notes = [];
      self.drawGraticule = () => {};
      self.drawNote = (_c, _w, _h, text) => { self.notes.push(text); };
      self.drawTrace = (_c, frame, index) => {
        self.drawn.push(frame.channels[index].channel);
      };
      self.channelColor = () => '#fff';
      self.console = { error() {}, write() {} };
      self.refreshMeasurements = () => {};
      self.runCommand = async () => ({ value: true });
      globalThis.document = {
        getElementById() { return { textContent: '', hidden: true }; },
      };
      return self;
    }
    function frame() {
      return {
        channels: [
          { channel: 'A', scaleVPerCount: 1, offsetV: 0 },
          { channel: 'B', scaleVPerCount: 1, offsetV: 0 },
        ],
        envelope: false, streaming: false, flags: 0,
        samplesPerChannel: 4, preTriggerSamples: 0, sampleIntervalNs: 1e6,
        overflowed: () => false,
        channelIndex(name) {
          return this.channels.findIndex((c) => c.channel === name);
        },
        counts() { return new Int16Array([0, 1, 2, 3]); },
      };
    }
    """

    def test_a_channel_not_yet_read_back_is_still_drawn(self):
        """The strips guess B is off. That guess must not hide a trace the
        capture actually carries."""
        out = _run_js(self.HARNESS + """
        const self = stand();
        self.channelState.get('B').enabled = false;
        self.drawTimeDomain({}, frame(), 100, 80);
        process.stdout.write(JSON.stringify(self.drawn));
        """)
        assert out == ["A", "B"]

    def test_disable_drops_the_trace_already_on_screen(self):
        out = _run_js(self.HARNESS + """
        const self = stand();
        await self.execute('disable B');
        self.drawTimeDomain({}, frame(), 100, 80);
        const b = self.channelState.get('B');
        process.stdout.write(JSON.stringify({
          drawn: self.drawn, enabled: b.enabled, known: b.enabledKnown,
          checked: b.toggle.checked, dirty: self.dirty, persist: self.persist,
        }));
        """)
        assert out["drawn"] == ["A"]
        assert out["enabled"] is False
        assert out["known"] is True
        assert out["checked"] is False
        assert out["dirty"] is True
        assert out["persist"] is None

    def test_math_and_xy_treat_a_disabled_channel_as_off(self):
        out = _run_js(self.HARNESS + """
        const self = stand();
        self.setChannelEnabled('A', false);
        self.drawMath({}, frame(), { start: 0, end: 4 }, 100, 80, { expr: 'A-B' });
        self.drawXY({}, frame(), 100, 80);
        process.stdout.write(JSON.stringify(self.notes));
        """)
        assert out == [
            "Math A-B needs channels A and B on",
            "XY needs two channels on",
        ]


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

    def _draw(self, position_v, volts=(1.0,)):
        """Draw one channel at 1 V/div, moved `position_v` volts, and report
        the y it lands on. At 1 V/div a volt is a division."""
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
        // Counts at 1 V a count, so a count is the volt the test names.
        const counts = Int16Array.from(%s);
        const frame = {
          channels: [{ channel: 'A', scaleVPerCount: 1, offsetV: 0 }],
          flags: 0,
          samplesPerChannel: counts.length,
          preTriggerSamples: 0,
          sampleIntervalNs: 1,
          counts: () => counts,
          overflowed: () => false,
        };
        // The renderer's own helpers, with a hand-built canvas and state.
        const self = Object.assign(Object.create(ScopeApp.prototype), {
          ctx,
          // One pixel wide, so the trace is a single column and its y is
          // unambiguous.
          canvas: { width: 1, height: %d },
          channelState: new Map([['A', { voltsPerDiv: 1, positionV: %s }]]),
          showTriggerMarkers: false,
          extremes: { min: new Float32Array(0), max: new Float32Array(0) },
          display: {},
          drawGraticule() {},
        });
        ScopeApp.prototype.draw.call(self, frame);
        process.stdout.write(JSON.stringify(ys));
        """ % (json.dumps(list(volts)), self.HEIGHT, json.dumps(position_v)))

    def test_a_centred_channel_draws_a_volt_one_division_up(self):
        """The unshifted case, so a shift can be measured against it."""
        ys = self._draw(0)
        assert ys == [self.HEIGHT / 2 - 50] * len(ys)

    def test_shifting_up_two_volts_at_1v_per_div_moves_it_two_divisions_up(self):
        ys = self._draw(2)
        assert ys == [self.HEIGHT / 2 - 50 - 100] * len(ys)

    def test_shifting_down_moves_it_down(self):
        ys = self._draw(-1)
        assert ys == [self.HEIGHT / 2 - 50 + 50] * len(ys)

    def test_the_shift_is_in_volts_so_it_holds_through_a_scale_change(self):
        """2 V up is two divisions at 1 V/div and two fifths of one at 5.

        The control is labelled in volts, so the trace keeps its voltage when
        the scale changes rather than its place on the grid.
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
          const counts = Int16Array.from([0]);
          const frame = { channels: [{ channel: 'A', scaleVPerCount: 1, offsetV: 0 }],
            flags: 0, samplesPerChannel: 1, preTriggerSamples: 0, sampleIntervalNs: 1,
            counts: () => counts, overflowed: () => false };
          const self = Object.assign(Object.create(ScopeApp.prototype), {
            ctx, canvas: { width: 1, height: 400 },
            channelState: new Map([['A', { voltsPerDiv: perDiv, positionV: 2 }]]),
            showTriggerMarkers: false, drawGraticule() {},
            extremes: { min: new Float32Array(0), max: new Float32Array(0) },
            display: {},
          });
          ScopeApp.prototype.draw.call(self, frame);
          runs[perDiv] = ys[0];
        }
        process.stdout.write(JSON.stringify(runs));
        """)
        assert out["1"] == 200 - 100
        assert out["5"] == 200 - 20

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
          channelState: new Map([['A', { voltsPerDiv: 1, positionV: 2 }]]),
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
        assert "vertical position" in strip, (
            "each strip needs its own position field")
        assert "VERTICAL_UNITS" in strip
        assert "applyVerticalPosition" in strip

    def test_moving_a_trace_sends_nothing_to_the_scope(self):
        """The give-away that it is a view control and not a hardware one."""
        applier = SCOPE_JS.read_text().split(
            "  applyVerticalPosition(label, volts, {")[1].split("\n  /**")[0]
        for hardware in ["runCommand", "this.send", "set_offset"]:
            assert hardware not in applier, (
                "%s in applyVerticalPosition would move the measurements too" % hardware)
        assert "requestRedraw" in applier

    def _vertical(self, body):
        return _run_js("""
        const notes = [];
        const shown = [];
        const self = Object.assign(Object.create(ScopeApp.prototype), {
          channelState: new Map([['A', {
            voltsPerDiv: 1, positionV: 0, net: 'scope1',
            positionField: { show: (v) => shown.push(v) },
          }]]),
          requestRedraw() {},
          showVoltsPerDiv() {},
          console: { note: (t) => notes.push(t), write: (t) => notes.push(t) },
        });
        %s
        process.stdout.write(JSON.stringify({
          positionV: self.channelState.get('A').positionV, notes, shown }));
        """ % body)

    def test_the_reach_is_four_divisions_of_the_scale_and_a_clamp_is_said(self):
        out = self._vertical("self.applyVerticalPosition('A', 7);")
        assert out["positionV"] == 4
        assert out["shown"] == [4]
        assert len(out["notes"]) == 1 and "limited" in out["notes"][0]

    def test_a_smaller_scale_pulls_the_trace_in_and_says_so(self):
        """3 V fits at 1 V/div; at 0.5 V/div the edge is 2 V."""
        out = self._vertical("""
        self.applyVerticalPosition('A', 3);
        self.applyVoltsPerDiv('A', 0.5, { push: false });
        """)
        assert out["positionV"] == 2
        assert len(out["notes"]) == 1 and "limited" in out["notes"][0]

    def test_a_larger_scale_keeps_the_voltage(self):
        out = self._vertical("""
        self.applyVerticalPosition('A', 3);
        self.applyVoltsPerDiv('A', 5, { push: false });
        """)
        assert out["positionV"] == 3
        assert out["notes"] == []


@needs_node
class TestEachChannelKeepsItsColour:
    """A channel is one colour everywhere, chosen by its name.

    A capture carries only the channels that are on, so with A off, B is a
    frame's first channel -- and was drawn in A's colour, beside a strip
    swatch and a trigger level in B's.
    """

    PALETTE = """
    const COLORS = { '--ch-a': '#aaa', '--ch-b': '#bbb', '--ch-c': '#ccc',
                     '--ch-d': '#ddd', '--ch-math': '#eee' };
    globalThis.getComputedStyle = () => ({ getPropertyValue: (name) => COLORS[name] || '' });
    """

    def _strokes(self, display, labels=("B",)):
        """Draw a capture of only `labels`; report the colour of every stroke."""
        return _run_js(self.PALETTE + """
        const strokes = [];
        const context = () => ({
          clearRect() {}, beginPath() {}, save() {}, restore() {}, translate() {},
          setTransform() {}, setLineDash() {}, fillRect() {}, fillText() {},
          drawImage() {}, moveTo() {}, lineTo() {},
          measureText: () => ({ width: 10 }),
          stroke() { strokes.push(this.strokeStyle); },
        });
        globalThis.document = {
          documentElement: {},
          getElementById: () => ({ hidden: false, textContent: '' }),
          createElement: () => ({ getContext: context }),
        };
        globalThis.window = { devicePixelRatio: 1 };
        const labels = %s;
        const counts = Int16Array.from({ length: 64 }, (_, i) => Math.round(100 * Math.sin(i / 3)));
        const frame = {
          channels: labels.map((channel) => ({ channel, scaleVPerCount: 0.01, offsetV: 0 })),
          flags: 0, samplesPerChannel: counts.length, preTriggerSamples: 0,
          sampleIntervalNs: 1000, counts: () => counts, overflowed: () => false,
          channelIndex: (label) => labels.indexOf(label),
        };
        const self = Object.assign(Object.create(ScopeApp.prototype), {
          ctx: context(), canvas: { width: 200, height: 100 },
          channelState: new Map(['A', 'B', 'C'].map((l) => [l, { voltsPerDiv: 1 }])),
          showTriggerMarkers: false, drawGraticule() {},
          extremes: { min: new Float32Array(0), max: new Float32Array(0) },
          spectrumCache: {}, display: %s,
        });
        ScopeApp.prototype.draw.call(self, frame);
        process.stdout.write(JSON.stringify(strokes));
        """ % (json.dumps(list(labels)), json.dumps(display)))

    def test_with_a_off_b_is_drawn_in_bs_colour(self):
        assert self._strokes({}) == ["#bbb"]

    def test_and_so_are_its_persisted_traces(self):
        strokes = self._strokes({"persistence": 2})
        assert strokes and set(strokes) == {"#bbb"}

    def test_and_so_is_its_spectrum(self):
        strokes = self._strokes({"fft": {"channel": "B"}})
        assert "#bbb" in strokes and "#aaa" not in strokes

    def test_xy_is_drawn_in_the_x_channels_colour(self):
        assert self._strokes({"xy": True}, labels=("B", "C")) == ["#bbb"]

    def test_bs_swatch_measurements_and_trigger_level_agree(self):
        """B's strip built first in the list, where a colour by place is A's;
        and the measurement panel names the channel it settled on."""
        out = _run_js(self.PALETTE + """
        const make = () => ({
          children: [], style: {}, classList: { add() {} },
          append(...kids) { this.children.push(...kids); },
          appendChild(kid) { this.children.push(kid); },
          replaceChildren(...kids) { this.children = kids; },
          addEventListener() {}, setAttribute() {},
        });
        const host = make();
        globalThis.document = {
          documentElement: {},
          createElement: make,
          createTextNode: (text) => ({ text }),
          getElementById: (id) => (id === 'measurements' ? host
            : { value: id === 'trigger-source' ? 'B' : '0.5' }),
        };
        const self = Object.assign(Object.create(ScopeApp.prototype), {
          net: 'pico1',
          channelState: new Map([
            ['A', { enabled: false, net: 'scope1', attenuation: 1 }],
            ['B', { enabled: true, net: 'scope2', attenuation: 1, voltsPerDiv: 1 }],
          ]),
          send: async () => ({ value: { vpp: 1 } }),
        });
        const strip = self.buildChannelStrip('B', 0, {});
        await self.refreshMeasurements();
        const strokes = [];
        const ctx = { save() {}, restore() {}, beginPath() {}, setLineDash() {},
          moveTo() {}, lineTo() {}, fillRect() {}, fillText() {},
          measureText: () => ({ width: 40 }),
          stroke() { strokes.push(this.strokeStyle); } };
        self.drawTriggerLevel(ctx, 800, 400);
        const [heading] = host.children;
        process.stdout.write(JSON.stringify({
          strip: strip.children[0].children[0].style.background,
          heading: [heading.children[0].style.background, heading.children[1].text],
          level: strokes,
        }));
        """)
        assert out == {
            "strip": "var(--ch-b)",
            "heading": ["var(--ch-b)", "Channel B"],
            "level": ["#bbb"],
        }


@needs_node
class TestARefusalFromTheScopeIsShown:
    """`coupling gnd` is in the grammar the terminal CLI shares, and refused
    by the daemon: a PicoScope has no ground coupling. The page adds no rule
    of its own for that, so the daemon's reason is what it must print."""

    def test_coupling_gnd_prints_the_daemons_reason(self):
        reason = ("Hardware error: this scope has no ground coupling, so GND "
                  "cannot be applied")
        out = _run_js("""
        const requests = [];
        globalThis.fetch = async (url, init) => {
          requests.push({ url, ...JSON.parse(init.body) });
          return { ok: false, status: 502,
                   json: async () => ({ success: false, error: %s }) };
        };
        const lines = [];
        const self = Object.assign(Object.create(ScopeApp.prototype), {
          net: 'pico1',
          channelNets: [{ name: 'scope2', pin: 2 }, { name: 'scope1', pin: 1 }],
          channelState: new Map([['A', { net: 'scope1' }], ['B', { net: 'scope2' }]]),
          console: {
            write: (text, kind = 'ok') => lines.push([kind, text]),
            error: (text) => lines.push(['error', text]),
          },
        });
        const body = await self.execute('coupling gnd');
        process.stdout.write(JSON.stringify({ requests, lines, body }));
        """ % json.dumps(reason))
        assert out["requests"] == [{
            "url": "/net/command", "netname": "scope1",
            "action": "set_coupling", "params": {"mode": "gnd"},
        }]
        assert out["lines"] == [["error", reason]]
        assert out["body"] is None


@needs_node
class TestTheWindowCanBeMovedInTime:
    """Signal past the left or right edge was never sampled.

    So unlike the vertical position this cannot be done in the renderer: it
    has to ask the scope for a window somewhere else, which is the
    pre/post-trigger split.
    """

    def _apply(self, seconds, per_div=1e-3, push=True):
        """Call applyTimePosition and report what it sent, showed and said."""
        return _run_js("""
        const sent = [];
        const shown = [];
        const notes = [];
        const fields = { timebase: { value: String(%s) } };
        globalThis.document = {
          getElementById: (id) => fields[id] || { value: '' },
        };
        const self = {
          dirty: false,
          requestRedraw() { this.dirty = true; },
          timePositionField: { show: (v) => shown.push(v) },
          console: { note: (t) => notes.push(t) },
          runCommand: async (action, params) => { sent.push({ action, params }); },
        };
        const applied = await ScopeApp.prototype.applyTimePosition.call(
          self, %s, { push: %s });
        process.stdout.write(JSON.stringify(
          { sent, shown, notes, applied, dirty: self.dirty, held: self.timePositionS }));
        """ % (json.dumps(per_div), json.dumps(seconds), json.dumps(push)))

    def test_seconds_go_to_the_box_as_seconds(self):
        out = self._apply(2e-3, per_div=1e-3)
        assert out["sent"] == [
            {"action": "set_time_offset", "params": {"offset": 2e-3}}]

    def test_looking_back_sends_a_negative_offset(self):
        out = self._apply(-1.5e-3, per_div=1e-3)
        assert out["sent"][0]["params"]["offset"] == pytest.approx(-1.5e-3)

    def test_the_travel_stops_at_the_edge_of_the_block_and_says_so(self):
        """Past 5 divisions the trigger is off the screen and the split is
        already all-pre or all-post; asking for more cannot be honoured."""
        out = self._apply(1, per_div=1e-3)
        assert out["sent"][0]["params"]["offset"] == pytest.approx(5e-3)
        assert out["shown"] == [pytest.approx(5e-3)]
        assert len(out["notes"]) == 1 and "limited" in out["notes"][0]
        assert self._apply(-1, per_div=1e-3)["applied"] == pytest.approx(-5e-3)

    def test_the_reach_follows_the_timebase(self):
        assert self._apply(1, per_div=1e-6)["applied"] == pytest.approx(5e-6)

    def test_the_field_shows_what_was_actually_applied(self):
        out = self._apply(2.5e-3)
        assert out["shown"] == [2.5e-3]
        assert out["held"] == 2.5e-3
        assert out["notes"] == []

    def test_the_plot_is_redrawn(self):
        assert self._apply(1e-3)["dirty"] is True

    def test_a_readback_does_not_push_the_value_back(self):
        """Adopting the daemon's own offset must not re-arm the scope."""
        out = self._apply(2e-3, push=False)
        assert out["sent"] == []
        assert out["shown"] == [2e-3], "still shown, just not re-sent"

    def test_a_readback_is_shown_as_the_box_has_it(self):
        """Clamping a value the box holds would show a window it is not
        capturing; only what the page sends is held to the screen."""
        out = self._apply(1, per_div=1e-3, push=False)
        assert out["shown"] == [1]
        assert out["notes"] == []

    def test_the_control_exists_with_a_unit(self):
        html = (SCOPE_DIR / "index.html").read_text()
        assert 'id="time-position"' in html
        assert 'id="time-position-unit"' in html
        assert 'id="time-position-reset"' in html

    def test_a_new_timebase_resends_the_position(self):
        """The box turns seconds into a split of the window in force when
        they arrive, so at a new time/div the old split is a different time,
        and the reach may have shrunk."""
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

    @needs_node
    @pytest.mark.parametrize("connection", [
        "{}",
        "{ connecting: true }",
        "{ socket: { readyState: 0, close() {} } }",
        "{ socket: { readyState: 1, send() {}, close() {} } }",
    ], ids=["never-connected", "fetching-ticket", "socket-opening", "open"])
    def test_disconnecting_stops_the_timer(self, connection):
        """Otherwise it outlives the socket, taking a capture every half
        second against a scope nobody is watching -- including on a page that
        never connected."""
        out = _run_js("""
        const cleared = [];
        globalThis.clearInterval = (id) => cleared.push(id);
        globalThis.document = { getElementById: () => ({}) };
        const self = Object.assign(Object.create(ScopeApp.prototype),
          { measureTimer: 7, socket: null, connecting: false, connectAttempt: 0 },
          %s);
        self.disconnect();
        process.stdout.write(JSON.stringify({ cleared, timer: self.measureTimer }));
        """ % connection)
        assert out == {"cleared": [7], "timer": None}


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

    def test_every_offered_step_is_one_the_unit_can_hit_exactly(self):
        """The list was a 1-2-5 ladder clamped at the fast end, so most of what
        it offered was unreachable and got rounded: on the 2204A on the bench,
        50 us/div lands on 64, 5 ms/div on 4.096, and 10 us/div on 8. The
        dropdown then corrected itself to the achieved value after every
        change, inserting an off-ladder entry and rebuilding the list under
        whoever was using it -- which is why the control seemed to ignore a
        change, snap back, or apply the previous one.

        A PicoScope's interval doubles per timebase step, so the reachable set
        is the fastest screen time times powers of two and nothing between.
        """
        offered = self._choices(self.CAPS, self.DEPTH)
        fastest = self.DEPTH / (self.CAPS["max_sample_rate_hz"] * 10)

        for step in offered:
            ratio = step / fastest
            power = round(math.log2(ratio))
            assert abs(ratio - 2 ** power) < 1e-9, (
                "%g s/div is not %g doubled a whole number of times" % (
                    step, fastest))

    def test_the_steps_measured_on_the_bench_are_offered(self):
        """Read off the 2204A by asking for each 1-2-5 value and recording what
        `get_timebase` reported back."""
        offered = self._choices(self.CAPS, self.DEPTH)
        for measured in (8e-6, 1.6e-5, 6.4e-5, 1.28e-4, 2.56e-4, 5.12e-4,
                         1.024e-3, 2.048e-3, 4.096e-3, 8.192e-3, 1.6384e-2):
            assert any(abs(o - measured) < 1e-12 for o in offered), (
                "%g s/div was measured as reachable" % measured)

    def test_the_ladder_is_not_the_one_two_five_one(self):
        """Guards the regression directly: these are round numbers the unit
        cannot reach, and offering them is what forced the correction."""
        offered = self._choices(self.CAPS, self.DEPTH)
        for unreachable in (1e-5, 5e-5, 1e-4, 1e-3, 5e-3, 1e-2):
            assert unreachable not in offered

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

    def test_the_achieved_value_selects_the_step_it_already_is(self):
        """The daemon reports time/div as depth x interval / 1e9 / 10, so
        1.024 ms/div comes back as 0.0010240000000000002. Matched as text it
        was never on the list, and each readback added a second entry for a
        step that was already there."""
        out = _run_js("""
        const { timebaseChoices } = await import(%s);
        const app = Object.create(ScopeApp.prototype);
        const offered = timebaseChoices(%s, %d).map(String);
        const options = offered.map((value) => ({ value }));
        const select = {
          options, value: '',
          add(option, before) {
            const at = before ? options.indexOf(before) : options.length;
            options.splice(at < 0 ? options.length : at, 0, option);
          },
        };
        globalThis.document = { getElementById: () => select };
        const results = offered.map((value, k) => {
          // The 2204A's interval doubles from 10 ns with each step.
          const readback = %d * (10 * 2 ** k) / 1e9 / 10;
          app.showTimebase(readback);
          return { offered: value, readback: String(readback), shown: select.value };
        });
        process.stdout.write(JSON.stringify({
          offered, results, listed: options.map((o) => o.value),
        }));
        """ % (json.dumps(str(SCOPE_JS)), json.dumps(self.CAPS), self.DEPTH,
               self.DEPTH))
        assert out["listed"] == out["offered"], "a step was listed twice"
        assert [r["shown"] for r in out["results"]] == out["offered"]
        # Not vacuous: the readbacks really are different text.
        assert all(r["readback"] != r["offered"] for r in out["results"])

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
