# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
The page sending each command to the net that owns the setting.

The dropdown used to list channels, so the timebase, the trigger and
run/stop -- none of which belong to a channel -- were sent to whichever one
was selected, and the three read-backs were taken from `anyNet`, the first
channel strip that happened to have a net wired. That gave right answers by
accident: the settings are device-wide however you reach them.

Now the dropdown selects the scope and the strips come from its channel nets,
so the routing has to be decided per command rather than per selection. The
danger in that is quiet: a per-channel action misrouted to the scope net is
refused and visible, but a device-wide one misrouted to a channel succeeds
and looks fine, so the two classifications must agree with the box's.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

SCOPE_JS = (pathlib.Path(__file__).resolve().parents[3]
            / "box" / "lager" / "static" / "scope" / "scope.js")


def _run_js(body: str):
    """Run a snippet against scope.js in node, returning its JSON output."""
    script = (
        "import { ScopeApp, PER_CHANNEL_ACTIONS, isPerChannelAction } "
        "from '%s';\n"
        "const main = async () => {\n%s\n};\nmain();\n" % (SCOPE_JS, body)
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        pytest.fail(proc.stderr.strip())
    return json.loads(proc.stdout) if proc.stdout.strip() else None


NETS = json.dumps([
    {"name": "pico1", "role": "scope", "instrument": "picoscope_2000",
     "address": "usb::one"},
    {"name": "vbus", "role": "scope-channel", "instrument": "picoscope_2000",
     "address": "usb::one", "pin": 1},
    {"name": "reset", "role": "scope-channel", "instrument": "picoscope_2000",
     "address": "usb::one", "pin": 2},
    {"name": "rigol1", "role": "scope", "instrument": "Rigol_MSO5204",
     "address": "usb::two"},
    {"name": "clk", "role": "scope-channel", "instrument": "Rigol_MSO5204",
     "address": "usb::two", "pin": 1},
])


class TestEachCommandGoesToTheNetThatOwnsIt:

    def _routed(self, action, channels="[{ name: 'vbus', pin: 1 }]"):
        return _run_js("""
        globalThis.fetch = async (url, init) =>
          ({ ok: true, json: async () => ({ netname: JSON.parse(init.body).netname }) });
        const self = {
          net: 'pico1',
          channelNets: %s,
          netForAction: ScopeApp.prototype.netForAction,
        };
        const body = await ScopeApp.prototype.send.call(self, '%s', {});
        process.stdout.write(JSON.stringify(body.netname));
        """ % (channels, action))

    @pytest.mark.parametrize("action", [
        "set_timebase", "get_timebase", "set_time_offset", "get_time_offset",
        "start_capture", "stop_capture", "start_single", "force_trigger",
        "trigger_edge", "capabilities",
        "set_cursor", "get_cursor", "clear_cursor", "measure_cursor",
    ])
    def test_device_wide_commands_go_to_the_scope(self, action):
        assert self._routed(action) == "pico1"

    @pytest.mark.parametrize("action", [
        "enable_net", "disable_net", "get_net_enabled",
        "set_scale", "get_scale", "set_coupling", "get_coupling",
        "set_probe", "get_probe", "set_offset", "get_offset",
        "measure_all", "measure_vpp", "measure_freq", "fft",
    ])
    def test_per_channel_commands_go_to_a_channel(self, action):
        assert self._routed(action) == "vbus"

    def test_an_explicit_net_still_wins(self):
        """The channel strips pass their own, and must keep reaching it."""
        out = _run_js("""
        globalThis.fetch = async (url, init) =>
          ({ ok: true, json: async () => ({ netname: JSON.parse(init.body).netname }) });
        const self = {
          net: 'pico1',
          channelNets: [{ name: 'vbus', pin: 1 }],
          netForAction: ScopeApp.prototype.netForAction,
        };
        const body = await ScopeApp.prototype.send.call(
          self, 'set_coupling', {}, 'reset');
        process.stdout.write(JSON.stringify(body.netname));
        """)
        assert out == "reset"

    def test_measure_cursor_is_the_scopes_despite_its_name(self):
        """Cursors belong to the instrument, and this one reads them against
        the channel they were placed on rather than a net's own."""
        assert self._routed("measure_cursor") == "pico1"

    @pytest.mark.parametrize("channels", [
        "[{ name: 'reset', pin: 2 }, { name: 'vbus', pin: 1 }]",
        "[{ name: 'reset', pin: '2' }, { name: 'vbus', pin: '1' }]",
        "[{ name: 'reset', pin: 'B' }, { name: 'vbus', pin: 'CH1' }]",
    ])
    def test_with_no_channel_named_it_is_the_lowest_channel(self, channels):
        """By pin, as the strips are, not by the order the nets were saved
        in: with channel B's saved first, a bare `scale 0.5` set channel B."""
        assert self._routed("set_scale", channels) == "vbus"

    def test_with_no_pins_at_all_the_list_decides(self):
        assert self._routed(
            "set_scale", "[{ name: 'reset' }, { name: 'vbus' }]") == "reset"


class TestTheDropdownSelectsAScope:

    def _loaded(self, nets=NETS):
        return _run_js("""
        const options = [];
        globalThis.fetch = async () => ({ ok: true, json: async () => ({ nets: %s }) });
        globalThis.document = { getElementById: () => ({
          replaceChildren: () => {},
          append: (o) => options.push(o.value),
          set value(v) { this._v = v; }, get value() { return this._v; },
        }) };
        globalThis.Option = function (label, value) { this.value = value; };
        const lines = [];
        const self = {
          console: {
            error: (text) => lines.push(['error', text]),
            note: (text) => lines.push(['note', text]),
          },
          adoptChannelNets: ScopeApp.prototype.adoptChannelNets,
          loadCapabilities: async () => {},
        };
        await ScopeApp.prototype.loadNets.call(self);
        process.stdout.write(JSON.stringify({
          options,
          selected: self.net,
          channels: (self.channelNets || []).map((n) => n.name),
          lines,
        }));
        """ % nets)

    def test_only_picoscopes_are_offered(self):
        """The box refuses a stream ticket for any other make."""
        assert self._loaded()["options"] == ["pico1"]

    def test_a_scope_of_another_make_is_named_instead(self):
        out = self._loaded()
        notes = [text for kind, text in out["lines"] if kind == "note"]
        assert any("rigol1" in text and "PicoScope" in text for text in notes)
        assert not [text for kind, text in out["lines"] if kind == "error"]

    def test_a_box_with_only_another_make_says_so(self):
        rigol = json.dumps([n for n in json.loads(NETS) if "Rigol" in n["instrument"]])
        out = self._loaded(rigol)
        assert out["options"] == [""]
        (kind, text), = out["lines"]
        assert kind == "error"
        assert "rigol1" in text and "PicoScope" in text
        assert "clk" not in text

    def test_its_channels_are_adopted(self):
        assert self._loaded()["channels"] == ["vbus", "reset"]

    def test_the_other_scopes_channels_are_not(self):
        """A bench can hold two, and their channels must not mix."""
        assert "clk" not in self._loaded()["channels"]

    def test_a_box_with_no_scope_net_still_works(self):
        """Nothing added since the roles split, so nothing has migrated."""
        legacy = json.dumps([
            {"name": "scope1", "role": "scope", "instrument": "picoscope_2000", "pin": 1},
            {"name": "scope2", "role": "scope", "instrument": "picoscope_2000", "pin": 2},
        ])
        out = self._loaded(legacy)
        assert out["options"] == ["scope1", "scope2"]
        assert out["selected"] == "scope1"

    ANALOG = {"name": "old_scope", "role": "analog",
              "instrument": "picoscope_2000", "address": "usb::one", "pin": 1}

    def test_an_analog_net_is_not_offered_and_the_console_says_why(self):
        """/net/command has no handler for the analog role: offered, every
        control on the page came back refused."""
        out = self._loaded(json.dumps(json.loads(NETS) + [self.ANALOG]))
        assert out["options"] == ["pico1"]
        assert out["selected"] == "pico1"
        assert out["channels"] == ["vbus", "reset"]
        notes = [text for kind, text in out["lines"] if kind == "note"]
        assert any("old_scope" in text and "analog" in text for text in notes)
        assert not [text for kind, text in out["lines"] if kind == "error"]

    def test_an_analog_net_is_not_taken_for_a_channel_either(self):
        """A box with no scope-channel nets uses the others as channels."""
        legacy = json.dumps([
            {"name": "scope1", "role": "scope", "instrument": "picoscope_2000", "pin": 1},
            dict(self.ANALOG, address=""),
        ])
        out = self._loaded(legacy)
        assert out["options"] == ["scope1"]
        assert "old_scope" not in out["channels"]

    def test_a_box_with_only_analog_nets_says_what_to_do(self):
        out = self._loaded(json.dumps([self.ANALOG]))
        assert out["options"] == [""]
        assert out.get("selected") is None
        (kind, text), = out["lines"]
        assert kind == "error"
        assert "old_scope" in text and "lager nets add" in text


class TestTheTwoClassificationsAgree:
    """The box refuses a per-channel action on the scope net, so a name the
    page thinks is device-wide and the box thinks is not fails visibly. The
    other way round does not: a device-wide action sent to a channel net is
    accepted, so a name missing here would never be noticed."""

    def test_the_lists_are_identical(self):
        from lager.http_handlers.net_command import _PER_CHANNEL_SCOPE_ACTIONS

        page = set(_run_js(
            "process.stdout.write(JSON.stringify([...PER_CHANNEL_ACTIONS]));"))
        assert page == set(_PER_CHANNEL_SCOPE_ACTIONS)

    def test_every_measurement_the_box_has_is_per_channel_here(self):
        from lager.http_handlers.net_command import _SCOPE_MEASUREMENTS

        actions = json.dumps(sorted(_SCOPE_MEASUREMENTS))
        out = _run_js(
            "process.stdout.write(JSON.stringify(%s.filter((a) => "
            "!isPerChannelAction(a))));" % actions)
        assert out == []

    def test_nothing_device_wide_is_treated_as_per_channel(self):
        device_wide = json.dumps([
            "set_timebase", "get_timebase", "set_time_offset",
            "get_time_offset", "start_capture", "stop_capture",
            "start_single", "force_trigger", "trigger_edge", "capabilities",
            "set_cursor", "get_cursor", "clear_cursor", "measure_cursor",
            "autoscale",
        ])
        out = _run_js(
            "process.stdout.write(JSON.stringify(%s.filter("
            "isPerChannelAction)));" % device_wide)
        assert out == []


class TestTheMeasurementPanelPicksAChannel:
    """The dropdown selects the scope, which has no channel to measure."""

    def _panel(self, states):
        return _run_js("""
        const self = { channelState: new Map(%s) };
        const picked = ScopeApp.prototype.measuredChannel.call(self);
        process.stdout.write(JSON.stringify(picked));
        """ % states)

    def test_it_reads_the_first_channel_that_is_on(self):
        picked = self._panel(
            "[['A', { enabled: true, net: 'vbus' }], "
            " ['B', { enabled: true, net: 'reset' }]]")
        assert picked["label"] == "A" and picked["net"] == "vbus"

    def test_it_moves_on_when_that_one_is_switched_off(self):
        """It used to sit on the selected net and report the channel dead."""
        picked = self._panel(
            "[['A', { enabled: false, net: 'vbus' }], "
            " ['B', { enabled: true, net: 'reset' }]]")
        assert picked["label"] == "B"

    def test_a_channel_with_no_net_cannot_be_measured(self):
        picked = self._panel(
            "[['A', { enabled: true, net: null }], "
            " ['B', { enabled: true, net: 'reset' }]]")
        assert picked["net"] == "reset"

    def test_none_on_reports_nothing_rather_than_guessing(self):
        assert self._panel("[['A', { enabled: false, net: 'vbus' }]]") is None
