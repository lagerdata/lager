# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the `lager scope` settings a bench scope has on its panel
(cli/commands/measurement/scope.py): status, the trigger readback, holdoff,
acquisition, roll, the spectrum and the display.

Each is an action on the box's warm handler, so what the CLI owns is which
action a command line becomes and which parameters go with it. The box is
mocked at the ``post_net_command`` boundary.

The edge trigger is here too, for the one way it differs by instrument: a
PicoScope's goes to the warm handler, which reports a refused setting as a
failure. The script it ran before printed "Trigger configured successfully"
for a level beyond the range, and ignored --source.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

scope_mod = importlib.import_module("cli.commands.measurement.scope")


class _Obj:
    """Settable stand-in for the LagerContext (the group stashes attrs on it)."""


def _run(args, instrument="picoscope_2000"):
    """Invoke `lager scope ...` with the box mocked.

    Returns (result, warm calls, exec calls).
    """
    calls: list[dict] = []
    executed: list[dict] = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False,
                  http_timeout=None, **params):
        calls.append({"netname": netname, "action": action, "role": role,
                      "params": params})
        return {"success": True, "message": "ok"}

    def fake_exec(ctx, path, box_ip, env=(), **kwargs):
        executed.append({"env": env})

    with patch.object(scope_mod, "post_net_command", fake_post), \
         patch.object(scope_mod, "run_python_internal", fake_exec), \
         patch.object(scope_mod, "resolve_box_locked", lambda *a, **k: "1.2.3.4"), \
         patch.object(scope_mod, "validate_net_exists",
                      lambda ctx, ip, name, role: {"name": name, "instrument": instrument}), \
         patch.object(scope_mod, "get_default_net", lambda ctx, t: None):
        result = CliRunner().invoke(scope_mod.scope, args, obj=_Obj(),
                                    catch_exceptions=False)
    return result, calls, executed


def _only_call(calls):
    assert len(calls) == 1, calls
    return calls[0]["action"], calls[0]["params"]


@pytest.mark.parametrize("args,action,params", [
    (["status"], "get_state", {}),
    (["trigger"], "get_trigger", {}),
    (["trigger", "holdoff"], "get_trigger_holdoff", {}),
    (["trigger", "holdoff", "0.001"], "set_trigger_holdoff", {"seconds": 0.001}),
    (["acquire"], "get_acquire", {}),
    (["acquire", "normal"], "set_acquire", {"mode": "normal"}),
    (["acquire", "average", "--count", "64"], "set_acquire", {"mode": "average", "count": 64}),
    (["acquire", "peak"], "set_acquire", {"mode": "peak"}),
    (["roll"], "get_roll", {}),
    (["roll", "on"], "set_roll", {"mode": "on"}),
    (["fft"], "fft", {"window": "hann", "peaks": 5}),
    (["fft", "--channel", "B", "--window", "flattop", "--peaks", "3"], "fft",
     {"channel": "B", "window": "flattop", "peaks": 3}),
    (["display"], "get_display", {}),
    (["display", "persistence", "2"], "set_display", {"persistence": "2"}),
    (["display", "persistence", "infinite"], "set_display", {"persistence": "infinite"}),
    (["display", "xy", "on"], "set_display", {"xy": "on"}),
    (["display", "zoom", "8", "--center", "0.001"], "set_display",
     {"zoom": {"factor": 8.0, "center": 0.001}}),
    (["display", "zoom", "off"], "set_display", {"zoom": "off"}),
    (["display", "math", "a-b"], "set_display", {"math": "a-b"}),
    (["display", "fft", "a"], "set_display", {"fft": {"channel": "a", "window": "hann"}}),
    (["display", "fft", "off"], "set_display", {"fft": "off"}),
])
def test_each_command_line_is_one_action_on_the_box(args, action, params):
    result, calls, executed = _run(["pico1", *args, "--box", "B"])
    assert result.exit_code == 0, result.output
    assert _only_call(calls) == (action, params)
    assert calls[0]["netname"] == "pico1"
    assert executed == []


@pytest.mark.parametrize("args", [
    ["trigger", "holdoff", "11"],
    ["acquire", "average", "--count", "0"],
    ["acquire", "smooth"],
    ["roll", "sometimes"],
    ["fft", "--window", "kaiser"],
    ["display", "xy", "maybe"],
])
def test_a_value_outside_its_range_is_refused_before_the_box(args):
    result, calls, _ = _run(["pico1", *args, "--box", "B"])
    assert result.exit_code != 0
    assert calls == []


def test_a_count_without_a_mode_is_refused():
    result, calls, _ = _run(["pico1", "acquire", "--count", "8", "--box", "B"])
    assert result.exit_code == 1
    assert "needs a mode" in result.output
    assert calls == []


def test_a_zoom_that_is_not_a_number_is_refused_without_a_traceback():
    result, calls, _ = _run(["pico1", "display", "zoom", "lots", "--box", "B"])
    assert result.exit_code == 1
    assert "zoom is a factor" in result.output
    assert calls == []


class TestTheEdgeTrigger:

    def test_a_picoscope_sends_only_what_was_named(self):
        result, calls, executed = _run(
            ["pico1", "trigger", "edge", "--level", "0.5", "--source", "B", "--box", "X"])
        assert result.exit_code == 0, result.output
        assert _only_call(calls) == ("trigger_edge", {"level": 0.5, "source": "B"})
        assert executed == []

    def test_a_picoscope_edge_with_nothing_named_is_refused(self):
        result, calls, executed = _run(["pico1", "trigger", "edge", "--box", "X"])
        assert result.exit_code == 1
        assert "at least one" in result.output
        assert calls == [] and executed == []

    def test_a_rigol_keeps_the_script_path(self):
        result, calls, executed = _run(
            ["rigol1", "trigger", "edge", "--level", "0.5", "--box", "X"],
            instrument="Rigol_MSO5204")
        assert result.exit_code == 0, result.output
        assert calls == []
        assert len(executed) == 1
        assert '"trigger_edge"' in executed[0]["env"][0]
