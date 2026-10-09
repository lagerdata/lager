# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
The in-browser scope CLI and the box handler must share one command vocabulary.

The point of these tests is to make drift impossible to merge. The web UI's
grammar lives in JavaScript (``static/scope/commands.js``) and the handler
lives in Python (``http_handlers/net_command.py``), so nothing in either
language forces them to agree -- a renamed action would leave the UI sending a
command the box rejects, and no unit test of either half would notice.

So these tests run the real JavaScript grammar under node, take the actions it
produces, and drive the real Python handler with them.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GRAMMAR_JS = REPO_ROOT / "box" / "lager" / "static" / "scope" / "commands.js"

# One line per verb the UI offers, in the spelling a user would type. Every
# one of these must parse in JS and be accepted by the Python handler.
COMMAND_LINES = [
    "enable",
    "enable A",
    "enable B",
    "disable",
    "disable B",
    "start",
    "start single",
    "stop",
    "force",
    "scale 0.5",
    "scale",
    "scale B 0.5",
    "scale B",
    "timebase 1e-3",
    "timebase",
    "coupling dc",
    "coupling",
    "coupling B ac",
    "coupling B",
    "probe 10",
    "probe",
    "probe B 10",
    "probe B",
    "offset 0.1",
    "offset",
    "offset B 0.1",
    "offset B",
    "hpos 2e-3",
    "hpos",
    "hpos -2e-3",
    "hpos 100ms",
    "hpos 0.1",
    "hpos 1e-3",
    "hpos 500us",
    "measure vpp",
    "measure vmax",
    "measure vmin",
    "measure vrms",
    "measure vavg",
    "measure freq",
    "measure period",
    "measure duty-pos",
    "measure duty-neg",
    "measure width-pos",
    "measure width-neg",
    "measure rise",
    "measure fall",
    "measure overshoot",
    "measure all",
    "measure A vpp",
    "measure B vpp",
    "measure 2 freq",
    "measure B all",
    "trigger",
    "trigger level 1.2 slope rising",
    "trigger edge level 0 source A",
    "trigger mode auto",
    "holdoff",
    "holdoff 1e-3",
    "acquire",
    "acquire normal",
    "acquire average 64",
    "acquire peak",
    "roll",
    "roll auto",
    "roll on",
    "roll off",
    "status",
    "display",
    "persistence 2",
    "persistence infinite",
    "persistence off",
    "xy on",
    "xy off",
    "zoom 8 1e-3",
    "zoom off",
    "math a-b",
    "math off",
    "fft a flattop",
    "fft off",
    "spectrum",
    "spectrum a 3",
    "cursor",
    "cursor time 1e-3 2e-3",
    "cursor volts 0.5 -0.5",
    "cursor off",
    "capabilities",
    "autoscale",
]

# Verbs the page answers itself, so they go through the grammar like the rest.
# They carry no action: there is nothing for the box handler to accept, and
# sending one would be the bug.
PAGE_LOCAL_LINES = [
    "vpos",
    "vpos B",
    "vpos B -0.5",
    "vpos -0.5",
    "vpos A 200mV",
    "sidebar",
    "sidebar on",
    "sidebar off",
    "sidebar show",
    "sidebar hide",
]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to run the browser command grammar")


def _parse_with_node(lines):
    """Parse each line with the real JS grammar, returning {line: {...}}."""
    # Input arrives through the environment rather than argv: under
    # `node -e` the argv layout differs from a script invocation, and an
    # env var sidesteps quoting entirely.
    script = """
    import { parse } from %s;
    const lines = JSON.parse(process.env.SCOPE_TEST_LINES);
    const out = {};
    for (const line of lines) {
      try {
        const parsed = parse(line);
        out[line] = {
          action: parsed.action, params: parsed.params, channel: parsed.channel,
          local: parsed.local,
        };
      } catch (e) {
        out[line] = { error: String(e.message) };
      }
    }
    process.stdout.write(JSON.stringify(out));
    """ % json.dumps(str(GRAMMAR_JS))

    import os
    env = dict(os.environ, SCOPE_TEST_LINES=json.dumps(lines))
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False, env=env)
    if result.returncode != 0:
        pytest.fail("node failed to run the grammar: %s" % result.stderr.strip())
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def parsed():
    return _parse_with_node(COMMAND_LINES)


def test_every_documented_command_parses(parsed):
    failures = {line: r["error"] for line, r in parsed.items() if "error" in r}
    assert not failures, "grammar rejected its own commands: %s" % failures
    assert not [line for line, r in parsed.items() if r["local"]], (
        "page-local verbs belong in PAGE_LOCAL_LINES")


def test_page_local_commands_parse_and_send_nothing():
    result = _parse_with_node(PAGE_LOCAL_LINES)
    failures = {line: r["error"] for line, r in result.items() if "error" in r}
    assert not failures, "grammar rejected its own commands: %s" % failures
    for line, r in result.items():
        assert r["local"] is True and r["action"] is None, line


def test_hpos_takes_seconds_in_any_spelling():
    result = _parse_with_node(["hpos 100ms", "hpos 0.1", "hpos 1e-3", "hpos 5us",
                               "hpos 2ns", "hpos 1s"])
    offsets = {line: r["params"]["offset"] for line, r in result.items()}
    assert offsets["hpos 100ms"] == pytest.approx(0.1)
    assert offsets["hpos 0.1"] == pytest.approx(0.1)
    assert offsets["hpos 1e-3"] == pytest.approx(1e-3)
    assert offsets["hpos 5us"] == pytest.approx(5e-6)
    assert offsets["hpos 2ns"] == pytest.approx(2e-9)
    assert offsets["hpos 1s"] == pytest.approx(1)


def test_a_prefix_without_its_unit_is_refused():
    """"100m" reads as milli or mega; a wrong guess moves the window 10^9."""
    result = _parse_with_node(["hpos 100m", "hpos 1Ms", "vpos A 1Mv", "hpos ms"])
    for line, r in result.items():
        assert "must be a number" in r["error"], line


def test_position_is_still_hpos_but_not_offered():
    """The old spelling parses for a release, and help does not list it."""
    result = _parse_with_node(["position 1e-3", "position"])
    assert result["position 1e-3"] == {
        "action": "set_time_offset", "params": {"offset": 1e-3},
        "channel": None, "local": False}
    assert result["position"]["action"] == "get_time_offset"

    out = _node_eval("""
    import { helpRows, complete, helpFor } from %s;
    process.stdout.write(JSON.stringify({
      usages: helpRows().map(([usage]) => usage.split(' ')[0]),
      completions: complete('p'),
      asked: helpFor('position'),
    }));
    """)
    assert "position" not in out["usages"]
    assert "hpos" in out["usages"] and "vpos" in out["usages"]
    assert "position" not in out["completions"]
    assert out["asked"][0].startswith("hpos")


def test_vpos_names_its_channel_as_the_other_settings_do():
    """Letter first, like "scale B 0.5"; after the value it is refused."""
    result = _parse_with_node(["vpos B -0.5", "vpos -0.5 B", "vpos 2", "vpos E 1"])
    assert result["vpos B -0.5"]["channel"] == "B"
    assert result["vpos B -0.5"]["params"] == {"volts": -0.5}
    assert "channel first" in result["vpos -0.5 B"]["error"]
    # A digit is the value, never channel B.
    assert result["vpos 2"]["channel"] is None
    assert result["vpos 2"]["params"] == {"volts": 2}
    assert "channel must be A-D" in result["vpos E 1"]["error"]


def test_help_tells_offset_and_vpos_apart():
    """One changes what is measured, the other only where it is drawn."""
    out = _node_eval("""
    import { helpFor } from %s;
    process.stdout.write(JSON.stringify({
      offset: helpFor('offset')[1], vpos: helpFor('vpos')[1] }));
    """)
    assert "hardware" in out["offset"] and "measurements change" in out["offset"]
    assert "vpos" in out["offset"]
    assert "Display only" in out["vpos"]
    assert "every measurement are unchanged" in out["vpos"]
    assert "reload" in out["vpos"]


def _node_eval(script):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script % json.dumps(str(GRAMMAR_JS))],
        capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        pytest.fail("node failed: %s" % result.stderr.strip())
    return json.loads(result.stdout)


def test_actions_are_accepted_by_the_box_handler(parsed):
    """Drive the Python handler with the actions the browser produces.

    A mock device stands in for the hardware; what is under test is whether
    the handler recognizes the action and reads the parameters the UI sent,
    not what the scope does with them.
    """
    from lager.http_handlers import net_command

    unknown = []
    missing_params = []

    for line, result in parsed.items():
        device = _MockScope()
        try:
            _invoke_scope(net_command, device, result["action"], result["params"])
        except net_command.UnknownAction:
            unknown.append((line, result["action"]))
        except KeyError as e:
            # The handler raises KeyError naming a parameter it required but
            # did not receive -- exactly the drift this test exists to catch.
            missing_params.append((line, result["action"], str(e)))

    assert not unknown, "handler does not implement actions the UI sends: %s" % unknown
    assert not missing_params, (
        "UI sends different parameter names than the handler reads: %s" % missing_params)


def test_measure_actions_cover_the_daemon_measurements(parsed):
    """Every measurement the UI offers must be one the handler maps."""
    from lager.http_handlers import net_command

    # `measure_all` and `measure_cursor` are spelled like the rest but are not
    # named quantities: one returns the whole set and the other reads the
    # cursors, so the handler implements them directly rather than through the
    # measurement table.
    not_a_quantity = {"measure_all", "measure_cursor"}
    ui_measurements = {
        r["action"] for r in parsed.values()
        if "action" in r and r["action"].startswith("measure_")
    } - not_a_quantity
    handler_measurements = set(net_command._SCOPE_MEASUREMENTS)

    assert ui_measurements <= handler_measurements, (
        "UI offers measurements the handler cannot perform: %s"
        % (ui_measurements - handler_measurements))


def test_help_names_one_command_in_every_form_a_user_tries():
    """`help trigger` and `trigger --help` ask about trigger.

    A bare `help` asks for the whole list. `trigger help` is not a help
    request: it looks like a trigger setting. A line that sets something is
    not one either, or `trigger level 1` would never reach the scope.
    """
    asked = _help_topics([
        "help",
        "help trigger",
        "trigger --help",
        "trigger help",
        "enable --help",
        "trigger level 1",
        "start",
    ])
    assert asked["help"] == ""
    assert asked["help trigger"] == "trigger"
    assert asked["trigger --help"] == "trigger"
    assert asked["trigger help"] is None
    assert asked["enable --help"] == "enable"
    assert asked["trigger level 1"] is None
    assert asked["start"] is None

    text = _help_for("trigger")
    assert text["usage"].startswith("trigger ")
    assert "slope" in text["usage"]
    assert _help_for("not-a-command") is None


def _help_topics(lines):
    script = """
    import { helpTopic } from %s;
    const lines = JSON.parse(process.env.SCOPE_TEST_LINES);
    const out = {};
    for (const line of lines) out[line] = helpTopic(line);
    process.stdout.write(JSON.stringify(out));
    """ % json.dumps(str(GRAMMAR_JS))
    import os
    env = dict(os.environ, SCOPE_TEST_LINES=json.dumps(lines))
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False, env=env)
    if result.returncode != 0:
        pytest.fail("node failed: %s" % result.stderr.strip())
    return json.loads(result.stdout)


def _help_for(name):
    script = """
    import { helpFor } from %s;
    const row = helpFor(process.env.SCOPE_HELP_FOR);
    process.stdout.write(JSON.stringify(row
      ? { usage: row[0], help: row[1] } : null));
    """ % json.dumps(str(GRAMMAR_JS))
    import os
    env = dict(os.environ, SCOPE_HELP_FOR=name)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False, env=env)
    if result.returncode != 0:
        pytest.fail("node failed: %s" % result.stderr.strip())
    return json.loads(result.stdout)


def test_measure_names_its_channel_by_net_not_by_parameter():
    """A channel reaches the box as the net the request goes to.

    The measure handlers read no `channel` parameter, so a letter carried as
    one would be dropped and the first channel measured instead.
    """
    result = _parse_with_node(
        ["measure vpp", "measure b vpp", "measure 2 freq", "measure B all"])

    assert result["measure vpp"]["channel"] is None
    assert result["measure b vpp"]["channel"] == "B"
    assert result["measure 2 freq"]["channel"] == "B"
    assert result["measure B all"] == {
        "action": "measure_all", "params": {}, "channel": "B", "local": False}
    for line in ("measure b vpp", "measure 2 freq", "measure B all"):
        assert result[line]["params"] == {}, line


def test_measure_refuses_a_channel_the_scope_cannot_have():
    result = _parse_with_node(["measure Z vpp", "measure 5 vpp", "measure A B vpp"])

    assert "channel must be A-D" in result["measure Z vpp"]["error"]
    assert "channel must be A-D" in result["measure 5 vpp"]["error"]
    assert "one channel" in result["measure A B vpp"]["error"]


def test_measure_names_its_channel_first():
    """As every verb that names a channel does, and as the terminal's
    `lager scope <net> measure vpp` names the net first."""
    result = _parse_with_node([
        "measure vpp B", "measure freq 2", "measure vpp A B",
        "measure B vpp extra", "measure B"])

    # The channel typed last: refused, with the line in the order to type.
    assert 'e.g. "measure B vpp"' in result["measure vpp B"]["error"]
    assert 'e.g. "measure B freq"' in result["measure freq 2"]["error"]
    assert 'e.g. "measure A vpp"' in result["measure vpp A B"]["error"]
    assert "channel first" in result["measure B vpp extra"]["error"]
    assert "measure what?" in result["measure B"]["error"]


def test_sidebar_reports_or_sets_and_sends_nothing():
    """The sidebar is page chrome. A letter in the request would be a channel
    the box then tried to measure."""
    result = _parse_with_node([
        "sidebar", "sidebar on", "sidebar off", "sidebar show", "sidebar hide",
        "SIDEBAR OFF"])

    assert result["sidebar"] == {
        "action": None, "params": {}, "channel": None, "local": True}
    assert result["sidebar on"]["params"] == {"shown": True}
    assert result["sidebar off"]["params"] == {"shown": False}
    assert result["sidebar show"]["params"] == {"shown": True}
    assert result["sidebar hide"]["params"] == {"shown": False}
    assert result["SIDEBAR OFF"]["params"] == {"shown": False}
    assert result["SIDEBAR OFF"]["local"] is True
    assert result["SIDEBAR OFF"]["action"] is None


def test_sidebar_refuses_anything_but_on_or_off():
    result = _parse_with_node(["sidebar left", "sidebar off now", "sidebar 1"])

    for line, row in result.items():
        assert "sidebar takes" in row["error"], line


def test_measure_knows_only_its_own_measurements():
    """A name that every JavaScript object has is not a measurement."""
    result = _parse_with_node(["measure constructor", "measure B __proto__"])

    for line in ("measure constructor", "measure B __proto__"):
        assert "unknown measurement" in result[line]["error"], line


def test_a_channel_setting_names_its_channel_first_and_by_letter():
    """A digit there is the value: "scale 2" is 2 V/div, not channel B."""
    result = _parse_with_node([
        "scale 2", "scale b 0.5", "scale A", "coupling C ac",
        "probe D 10", "offset B -0.1"])

    assert result["scale 2"] == {
        "action": "set_scale", "params": {"volts_per_div": 2}, "channel": None,
        "local": False}
    assert result["scale b 0.5"] == {
        "action": "set_scale", "params": {"volts_per_div": 0.5}, "channel": "B",
        "local": False}
    assert result["scale A"] == {
        "action": "get_scale", "params": {}, "channel": "A", "local": False}
    assert result["coupling C ac"]["channel"] == "C"
    assert result["coupling C ac"]["params"] == {"mode": "ac"}
    assert result["probe D 10"]["params"] == {"ratio": 10}
    assert result["offset B -0.1"]["params"] == {"offset": -0.1}


def test_a_channel_setting_refuses_a_channel_it_cannot_reach():
    result = _parse_with_node(
        ["scale E 0.5", "scale 0.5 B", "coupling ac B", "probe 10 B", "offset 0.1 B"])

    assert "channel must be A-D" in result["scale E 0.5"]["error"]
    # The channel after the value: taking the value and dropping the letter
    # would set the default channel and report success.
    for line in ("scale 0.5 B", "coupling ac B", "probe 10 B", "offset 0.1 B"):
        assert "channel first" in result[line]["error"], line


def test_unknown_command_is_rejected_with_a_helpful_message():
    result = _parse_with_node(["frobnicate", "measure nonsense", "scale abc"])

    assert "unknown command" in result["frobnicate"]["error"]
    # The message should list the alternatives rather than just refusing.
    assert "vpp" in result["measure nonsense"]["error"]
    assert "number" in result["scale abc"]["error"]


def _invoke_scope(net_command, device, action, params):
    """Call the scope handler with a mock device in place of the hardware."""
    original = net_command._proxy
    net_command._proxy = lambda *a, **k: device
    try:
        return net_command._scope("scope1", "scope-channel", action, params)
    finally:
        net_command._proxy = original


class _MockScope:
    """Accepts any driver call and returns a plausible value.

    Returns 0.0 for reads so the handler's float() conversions succeed; the
    values are irrelevant to what these tests check.
    """

    _CURSORS = {"time": [1e-3, 2e-3], "volts": None, "channel": "A"}
    _STATE = {
        "acquiring": True, "rolling": False, "capture_mode": "auto",
        "timebase": {"time_per_div": 1e-3, "time_offset": 0.0},
        "trigger": {"source": "A", "slope": "rising", "level": 0.5},
        "acquisition": {"mode": "normal", "average_count": 16},
        "channels": [{"channel": "A", "enabled": True, "volts_per_div": 1.0,
                      "coupling": "DC", "attenuation": 1.0}],
    }

    def __getattr__(self, name):
        def call(*_args, **_kwargs):
            if name == "capabilities":
                return {"model": "MOCK-2204A", "analog_channels": 2}
            # Cursors answer with their own shapes: the handler formats the
            # positions and readings out of them, so a bare float would fail
            # for a reason that has nothing to do with the vocabulary.
            if name in ("set_cursors", "get_cursors", "clear_cursors"):
                return dict(self._CURSORS)
            if name == "measure_cursors":
                return {"cursors": dict(self._CURSORS),
                        "readings": {"t1": 1e-3, "t2": 2e-3,
                                     "delta_t": 1e-3, "frequency": 1000.0}}
            # The rest of the settings the handler describes back.
            if name == "get_state":
                return dict(self._STATE)
            if name in ("set_acquisition", "get_acquisition"):
                return {"mode": "average", "average_count": 64}
            if name in ("set_roll", "get_roll"):
                return {"roll": "auto", "rolling": False}
            if name in ("set_display", "get_display"):
                return {"persistence": 2.0, "zoom": {"factor": 8.0, "center": 1e-3}}
            if name == "fft":
                return {"channel": "A", "window": "hann", "resolution_hz": 12.5,
                        "peaks": [{"frequency_hz": 1000.0, "vrms": 0.7, "dbv": -3.1}]}
            if name.startswith("get_") or name.startswith("measure"):
                return 0.0
            return {"status": "ok"}
        return call
