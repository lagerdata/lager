# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager scope` and `lager logic`: which path each subcommand takes, and what it
sends.

A scope subcommand goes one of two ways. Actions in `scope._WARM_ACTIONS` POST
to the box's in-process handler (:9000/net/command); the rest upload
`cli/impl/measurement/scope.py` (or `enable_disable.py`) through `lager python`
and pass their arguments in `LAGER_COMMAND_DATA`. Routing a Rigol-only action
to the warm path turns a working command into "unknown action"; routing a warm
action to the exec path makes it fail on a PicoScope. Neither shows up until
someone runs the command against an instrument, and no workflow does.

`test_scope_bench_commands.py` covers acquire/coupling/fft/roll/start/status/
display/stream capture|config|start|web/trigger holdoff. This file covers the
rest of the surface: the eleven measurements, the cursor group, timebase,
scale, probe, force, autoscale, enable/disable/stop and stream status/stop, and
every `lager logic` action.
"""

import pytest

SCOPE_MEASUREMENTS = [
    ("vpp", "measure_vpp"),
    ("vmax", "measure_vmax"),
    ("vmin", "measure_vmin"),
    ("vrms", "measure_vrms"),
    ("vavg", "measure_vavg"),
    ("period", "measure_period"),
    ("freq", "measure_freq"),
    ("pulse-width-pos", "measure_pulse_width_pos"),
    ("pulse-width-neg", "measure_pulse_width_neg"),
    ("duty-cycle-pos", "measure_dc_pos"),
    ("duty-cycle-neg", "measure_dc_neg"),
]


@pytest.fixture
def scope_box(fake_box):
    fake_box.net("scope1", "scope", instrument="rigol_mso5000")
    fake_box.net("logic1", "logic", instrument="rigol_mso5000")
    return fake_box


def _warm(box):
    url, body = box.posts[-1]
    assert url == f"http://{box.IP}:9000/net/command"
    return body


# ---------------------------------------------------------------------------
# scope measure
# ---------------------------------------------------------------------------

def test_every_measure_subcommand_is_covered_here():
    from cli.main import cli
    import click
    ctx = click.Context(cli)
    scope = cli.get_command(ctx, "scope")
    measure = scope.commands["measure"]
    assert sorted(measure.commands) == sorted(name for name, _ in SCOPE_MEASUREMENTS)


@pytest.mark.parametrize("sub, action", SCOPE_MEASUREMENTS)
def test_scope_measurement_takes_the_warm_path(lager, scope_box, sub, action):
    result = lager("scope", "scope1", "measure", sub)
    assert result.exit_code == 0, result.output
    assert _warm(scope_box) == {"netname": "scope1", "action": action, "params": {},
                                "role": "scope"}
    assert scope_box.execs == []


@pytest.mark.parametrize("flag", ["--display", "--cursor"])
@pytest.mark.parametrize("sub, action", SCOPE_MEASUREMENTS[:3])
def test_scope_measurement_on_screen_keeps_the_exec_path(lager, scope_box, sub, action, flag):
    # --display/--cursor draw on the instrument's own screen (Rigol only), so
    # they must not be silently dropped onto the warm path.
    result = lager("scope", "scope1", "measure", sub, flag)
    assert result.exit_code == 0, result.output
    assert scope_box.posts == []
    script, box, data = scope_box.last_exec
    assert script == "scope.py" and box == scope_box.IP
    assert data["action"] == action
    assert data["params"]["netname"] == "scope1"
    assert data["params"][flag.lstrip("-")] is True


def test_scope_measure_on_an_unknown_net_sends_nothing(lager, scope_box):
    result = lager("scope", "scope9", "measure", "vpp")
    assert result.exit_code != 0
    assert "scope9" in result.output
    assert scope_box.posts == [] and scope_box.execs == []


def test_scope_measure_box_error_is_a_non_zero_exit(lager, scope_box):
    scope_box.status = 500
    scope_box.answer = lambda url, body: {"success": False, "error": "no waveform captured"}
    result = lager("scope", "scope1", "measure", "freq")
    assert result.exit_code != 0
    assert "no waveform captured" in result.output


# ---------------------------------------------------------------------------
# scope settings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv, action, params", [
    (["enable"], "enable_net", {}),
    (["disable"], "disable_net", {}),
    (["stop"], "stop_capture", {}),
    (["start", "--single"], "start_single", {}),
    (["force"], "force_trigger", {}),
    (["autoscale"], "autoscale", {}),
    (["scale", "0.5"], "set_scale", {"volts_per_div": 0.5}),
    (["timebase", "0.001"], "set_timebase", {"seconds_per_div": 0.001}),
    (["probe", "10"], "set_probe", {"ratio": 10}),
    (["cursor", "off"], "clear_cursor", {}),
])
def test_scope_setting_payload(lager, scope_box, argv, action, params):
    result = lager("scope", "scope1", *argv)
    assert result.exit_code == 0, result.output
    assert _warm(scope_box) == {"netname": "scope1", "action": action, "params": params,
                                "role": "scope"}


@pytest.mark.parametrize("argv, message", [
    (["timebase", "0"], "Timebase must be between"),
    (["timebase", "100"], "Timebase must be between"),
])
def test_scope_out_of_range_settings_are_refused_locally(lager, scope_box, argv, message):
    result = lager("scope", "scope1", *argv)
    assert result.exit_code != 0
    assert message in result.output
    assert scope_box.posts == []


@pytest.mark.parametrize("argv", [["probe", "3"], ["scale", "-1"]])
def test_scope_invalid_argument_is_a_usage_error(lager, scope_box, argv):
    result = lager("scope", "scope1", *argv)
    assert result.exit_code == 2
    assert scope_box.posts == []


def test_scope_scale_zero_is_refused(lager, scope_box):
    result = lager("scope", "scope1", "scale", "0")
    assert result.exit_code != 0
    assert scope_box.posts == []


# ---------------------------------------------------------------------------
# scope cursor
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["time", "volts"])
def test_scope_cursor_pair_sets_then_measures(lager, scope_box, which):
    result = lager("scope", "scope1", "cursor", which, "0.001", "0.002")
    assert result.exit_code == 0, result.output
    actions = [body["action"] for _, body in scope_box.posts]
    assert actions == ["set_cursor", "measure_cursor"]
    assert scope_box.posts[0][1]["params"] == {which: {"1": 0.001, "2": 0.002}}


@pytest.mark.parametrize("argv, action, params", [
    (["set-a", "--x", "1", "--y", "2"], "set_a", {"x": 1.0, "y": 2.0}),
    (["set-b", "--x", "3", "--y", "4"], "set_b", {"x": 3.0, "y": 4.0}),
    (["move-a", "--x", "1"], "move_a", {"del_x": 1.0, "del_y": None}),
    (["move-b", "--y", "-2"], "move_b", {"del_x": None, "del_y": -2.0}),
    (["hide"], "hide_cursor", {}),
])
def test_scope_screen_cursor_uses_the_exec_path(lager, scope_box, argv, action, params):
    result = lager("scope", "scope1", "cursor", *argv)
    assert result.exit_code == 0, result.output
    assert scope_box.posts == []
    script, _, data = scope_box.last_exec
    assert script == "scope.py"
    assert data["action"] == action
    assert data["params"] == {"netname": "scope1", **params}


# ---------------------------------------------------------------------------
# scope stream status|stop
# ---------------------------------------------------------------------------

def test_scope_stream_status(lager, scope_box):
    result = lager("scope", "scope1", "stream", "status")
    assert result.exit_code == 0, result.output
    script, _, data = scope_box.last_exec
    assert script == "scope_stream.py"
    assert data["action"] == "stream_status"


def test_scope_stream_stop_names_the_net(lager, scope_box):
    result = lager("scope", "scope1", "stream", "stop")
    assert result.exit_code == 0, result.output
    script, _, data = scope_box.last_exec
    assert script == "scope_stream.py"
    assert data == {"action": "stream_stop", "params": {"netname": "scope1"}}


# ---------------------------------------------------------------------------
# lager logic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sub, action", [
    ("enable", "enable_net"),
    ("disable", "disable_net"),
    ("start", "start_capture"),
    ("start-single", "start_single"),
    ("stop", "stop_capture"),
])
def test_logic_basic_actions_go_to_enable_disable(lager, scope_box, sub, action):
    result = lager("logic", "logic1", sub)
    assert result.exit_code == 0, result.output
    script, box, data = scope_box.last_exec
    assert (script, box) == ("enable_disable.py", scope_box.IP)
    assert data["action"] == action
    assert data["params"] == {"netname": "logic1"}


@pytest.mark.parametrize("sub, action", [
    ("period", "measure_period"),
    ("freq", "measure_freq"),
    ("dc-pos", "measure_dc_pos"),
    ("dc-neg", "measure_dc_neg"),
    ("pw-pos", "measure_pulse_width_pos"),
    ("pw-neg", "measure_pulse_width_neg"),
])
def test_logic_measurements_carry_the_logic_role(lager, scope_box, sub, action):
    # Without role=logic, scope.py resolves the net as an analog channel and
    # never finds a logic net (#328).
    result = lager("logic", "logic1", "measure", sub)
    assert result.exit_code == 0, result.output
    script, _, data = scope_box.last_exec
    assert script == "scope.py"
    assert data["action"] == action
    assert data["params"]["role"] == "logic"
    assert data["params"]["netname"] == "logic1"
    assert data["params"]["display"] is False and data["params"]["cursor"] is False


def test_logic_measure_display_flag_is_forwarded(lager, scope_box):
    result = lager("logic", "logic1", "measure", "freq", "--display", "true")
    assert result.exit_code == 0, result.output
    assert scope_box.last_exec[2]["params"]["display"] is True


@pytest.mark.parametrize("argv, action, params", [
    (["set-a", "--x", "1", "--y", "2"], "set_a", {"x": 1.0, "y": 2.0}),
    (["set-b", "--x", "3", "--y", "4"], "set_b", {"x": 3.0, "y": 4.0}),
    (["move-a", "--del-x", "1"], "move_a", {"del_x": 1.0, "del_y": None}),
    (["move-b", "--del-y", "2"], "move_b", {"del_x": None, "del_y": 2.0}),
    (["hide"], "hide_cursor", {}),
])
def test_logic_cursor_actions(lager, scope_box, argv, action, params):
    result = lager("logic", "logic1", "cursor", *argv)
    assert result.exit_code == 0, result.output
    script, _, data = scope_box.last_exec
    assert script == "scope.py"
    assert data["action"] == action
    assert data["params"] == {"role": "logic", "netname": "logic1", **params}


def test_logic_on_a_scope_net_is_refused(lager, scope_box):
    result = lager("logic", "scope1", "enable")
    assert result.exit_code != 0
    assert scope_box.execs == []
