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


def _run_js(body):
    """Run `body` with ScopeApp imported, returning what it JSON-prints."""
    script = "import { ScopeApp } from %s;\n%s" % (json.dumps(str(SCOPE_JS)), body)
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
    const state = new Map([
      ['A', { enabled: true, voltsPerDiv: 1, net: 'scope1',
              toggle: { checked: true },
              select: { value: '1', options: [{value:'1'},{value:'0.5'}] } }],
      ['B', { enabled: false, voltsPerDiv: 1, net: 'scope2',
              toggle: { checked: false },
              select: { value: '1', options: [{value:'1'},{value:'0.5'}] } }],
    ]);
    const self = {
      channelState: state,
      send: async (action, params, net) => {
        calls.push({ action, net });
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

    def test_a_scale_the_dropdown_cannot_show_is_not_displayed(self):
        """Never show a value the hardware is not using."""
        out = self._sync(
            "(action) => action === 'get_net_enabled' "
            "? { value: true } : { value: 0.123 }",
            "{ perDiv: state.get('A').voltsPerDiv,"
            "  shown: state.get('A').select.value }")
        assert out["perDiv"] == 0.123
        assert out["shown"] == "1", "dropdown must not invent an option"

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
        const self = { net: 'scope1' };
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
          await ScopeApp.prototype.send.call({ net: null }, 'enable_net', {});
        } catch (e) { message = e.message; }
        process.stdout.write(JSON.stringify(message));
        """)
        assert out and "no scope net" in out


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
            result = net_command._scope("scope1", "scope", "get_net_enabled", {})
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
            result = net_command._scope("scope2", "scope", "get_net_enabled", {})
        finally:
            net_command._proxy = original

        assert result["value"] is False
        assert "disabled" in result["message"]
