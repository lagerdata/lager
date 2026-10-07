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

So are the parts of `stream` the CLI owns: the viewer link, which needs the
user's sign-in token on an access-gated box, the exit code of the box
script, and which channel a setting goes to.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

scope_mod = importlib.import_module("cli.commands.measurement.scope")
net_helpers = importlib.import_module("cli.core.net_helpers")
StreamDatatypes = importlib.import_module("cli.core.utils").StreamDatatypes


class _Obj:
    """Settable stand-in for the LagerContext (the group stashes attrs on it)."""


def _run(args, instrument="picoscope_2000", nets=None, script=(b"", b"", 0)):
    """Invoke `lager scope ...` with the box mocked.

    ``nets`` stands for the nets saved on the box; without it, every name is
    a net of ``instrument``. ``script`` is what an uploaded script prints on
    stdout and stderr, and the code it exits with, for a caller that reads
    them.

    Returns (result, warm calls, exec calls).
    """
    calls: list[dict] = []
    executed: list[dict] = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False,
                  http_timeout=None, **params):
        calls.append({"netname": netname, "action": action, "role": role,
                      "params": params})
        return {"success": True, "message": "ok"}

    def fake_exec(ctx, path, box_ip, env=(), callback=None, **kwargs):
        executed.append({"env": env})
        if callback is None:
            return None
        stdout, stderr, code = script
        context = None
        for datatype, content in ((StreamDatatypes.STDOUT, stdout),
                                  (StreamDatatypes.STDERR, stderr),
                                  (StreamDatatypes.EXIT, code)):
            done, context = callback(datatype, content, context)
            if done:
                return context
        return None

    def fake_validate(ctx, ip, name, role):
        if nets is None:
            return {"name": name, "instrument": instrument}
        return next((net for net in nets if net["name"] == name), None)

    def fake_list(*args):
        return list(nets or [])

    with patch.object(scope_mod, "post_net_command", fake_post), \
         patch.object(scope_mod, "run_python_internal", fake_exec), \
         patch.object(scope_mod, "resolve_box_locked", lambda *a, **k: "1.2.3.4"), \
         patch.object(scope_mod, "resolve_box", lambda *a, **k: "1.2.3.4"), \
         patch.object(scope_mod, "validate_net_exists", fake_validate), \
         patch.object(scope_mod, "run_net_py", fake_list), \
         patch.object(net_helpers, "run_net_py", fake_list), \
         patch.object(scope_mod, "get_default_net", lambda ctx, t: None):
        result = CliRunner().invoke(scope_mod.scope, args, obj=_Obj(),
                                    catch_exceptions=False)
    return result, calls, executed


def _only_call(calls):
    assert len(calls) == 1, calls
    return calls[0]["action"], calls[0]["params"]


def _sent(executed):
    """The command data the last script run was given."""
    return json.loads(executed[-1]["env"][0].split("=", 1)[1])


# One PicoScope with two channel nets, a channel net on a second PicoScope,
# and a net that is no scope's.
NETS = [
    {"name": "pico1", "role": "scope", "instrument": "Picoscope_2000",
     "address": "usb:1"},
    {"name": "probe_a", "role": "scope-channel", "instrument": "Picoscope_2000",
     "address": "usb:1", "pin": "1"},
    {"name": "probe_b", "role": "scope-channel", "instrument": "Picoscope_2000",
     "address": "usb:1", "pin": None, "mappings": [{"net": "probe_b", "pin": "2"}]},
    {"name": "far_probe", "role": "scope-channel", "instrument": "Picoscope_2000",
     "address": "usb:2", "pin": "1"},
    {"name": "vbus", "role": "power-supply", "instrument": "Keithley_2281S",
     "address": "usb:9", "pin": "1"},
]


@pytest.fixture
def ungated(monkeypatch):
    monkeypatch.setattr("cli.gateway_auth.auth_server_for_box", lambda ip: None)


def _gate(monkeypatch, token="tok"):
    """Make every box look access-gated, with ``token`` as the stored sign-in."""
    monkeypatch.delenv("LAGER_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr("cli.gateway_auth.auth_server_for_box",
                        lambda ip: "http://plane.example")
    monkeypatch.setattr("cli.gateway_auth.access_token_for", lambda url: token)
    monkeypatch.setattr("cli.gateway_auth._token_expires_at",
                        lambda tok: time.time() + 900)


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


class TestTheEdgeTriggerSource:
    """A PicoScope's --source takes a channel net, as a Rigol's does.

    The box reads the source as a channel letter or number only, so the CLI
    resolves a net name to the channel it is wired to.
    """

    @pytest.mark.parametrize("source,letter", [
        ("probe_a", "A"),
        ("probe_b", "B"),
        ("2", "B"),
        ("b", "B"),
        ("CH1", "A"),
    ])
    def test_a_channel_net_or_number_becomes_its_letter(self, source, letter):
        result, calls, _ = _run(
            ["pico1", "trigger", "edge", "--source", source, "--box", "X"], nets=NETS)
        assert result.exit_code == 0, result.output
        assert _only_call(calls) == ("trigger_edge", {"source": letter})

    def test_a_channel_net_can_trigger_on_its_sibling(self):
        result, calls, _ = _run(
            ["probe_b", "trigger", "edge", "--source", "probe_a", "--box", "X"], nets=NETS)
        assert result.exit_code == 0, result.output
        assert _only_call(calls) == ("trigger_edge", {"source": "A"})

    @pytest.mark.parametrize("source,message", [
        ("nope", "takes a channel"),
        ("vbus", "takes a channel"),
        ("far_probe", "different scope"),
        ("pico1", "not one of the scope's channels"),
    ])
    def test_a_source_that_is_no_channel_of_this_scope_is_refused(self, source, message):
        result, calls, _ = _run(
            ["pico1", "trigger", "edge", "--source", source, "--box", "X"], nets=NETS)
        assert result.exit_code == 1
        assert message in result.output
        assert calls == []


def test_the_bare_listing_includes_the_channel_nets():
    result, _, _ = _run(["--box", "B"], nets=NETS)
    assert result.exit_code == 0, result.output
    for name in ("pico1", "probe_a", "probe_b", "far_probe"):
        assert name in result.output
    assert "vbus" not in result.output


STARTED = (b"Streaming started\n", b"", 0)


class TestStreamStart:

    def test_a_gated_box_gets_a_link_with_the_sign_in_token(self, monkeypatch):
        _gate(monkeypatch, token="tok")
        result, _, _ = _run(["pico1", "stream", "start", "--box", "bench"],
                            nets=NETS, script=STARTED)
        assert result.exit_code == 0, result.output
        assert "Visualization: http://1.2.3.4:9000/scope?token=tok" in result.output
        assert "about 15 minutes" in result.output
        assert 'Run "lager scope pico1 stream web --box bench" for a fresh link.' \
            in result.output

    def test_a_gated_box_without_a_sign_in_points_at_login(self, monkeypatch):
        _gate(monkeypatch, token=None)
        result, _, _ = _run(["pico1", "stream", "start", "--box", "bench"],
                            nets=NETS, script=STARTED)
        assert result.exit_code == 0, result.output
        assert "Visualization: http://1.2.3.4:9000/scope\n" in result.output
        assert "lager login http://plane.example" in result.output

    def test_an_open_box_gets_the_plain_link_after_the_script_output(self, ungated):
        result, _, _ = _run(["pico1", "stream", "start", "--box", "bench"],
                            nets=NETS, script=STARTED)
        assert result.exit_code == 0, result.output
        assert result.output == ("Streaming started\n"
                                 "Visualization: http://1.2.3.4:9000/scope\n")

    def test_a_refused_start_fails_with_no_link(self, ungated):
        refusal = (b"", b"Error: cannot start acquisition: no scope connected\n", 1)
        result, _, _ = _run(["pico1", "stream", "start", "--box", "bench"],
                            nets=NETS, script=refusal)
        assert result.exit_code == 1
        assert "no scope connected" in result.output
        assert "Visualization" not in result.output

    def test_json_output_carries_the_link(self, monkeypatch):
        _gate(monkeypatch, token="tok")
        reply = json.dumps({"status": "success", "message": "Streaming started",
                            "command_port": 8085}).encode() + b"\n"
        result, _, _ = _run(["pico1", "stream", "start", "--json", "--box", "bench"],
                            nets=NETS, script=(reply, b"", 0))
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        assert payload["visualization_url"] == "http://1.2.3.4:9000/scope?token=tok"

    def test_quiet_prints_no_link(self, ungated):
        result, _, _ = _run(["pico1", "stream", "start", "-q", "--box", "bench"],
                            nets=NETS)
        assert result.exit_code == 0, result.output
        assert "Visualization" not in result.output

    @pytest.mark.parametrize("net,args,channel", [
        ("pico1", [], "A"),
        ("probe_b", [], "B"),
        ("probe_b", ["--channel", "A"], "A"),
    ])
    def test_the_channel_defaults_to_the_net_own(self, ungated, net, args, channel):
        result, _, executed = _run([net, "stream", "start", *args, "--box", "bench"],
                                   nets=NETS, script=STARTED)
        assert result.exit_code == 0, result.output
        params = _sent(executed)["params"]
        assert params["channel"] == channel
        assert "box_ip" not in params


class TestStreamWeb:

    def test_the_browser_gets_the_link_with_the_sign_in_token(self, monkeypatch):
        _gate(monkeypatch, token="tok")
        opened = []
        monkeypatch.setattr("webbrowser.open", opened.append)
        result, _, _ = _run(["pico1", "stream", "web", "--box", "bench"], nets=NETS)
        assert result.exit_code == 0, result.output
        assert opened == ["http://1.2.3.4:9000/scope?token=tok"]
        assert "lager scope pico1 stream web --box bench" in result.output

    def test_an_open_box_gets_the_plain_link(self, ungated, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", opened.append)
        result, _, _ = _run(["pico1", "stream", "web", "--box", "bench"], nets=NETS)
        assert result.exit_code == 0, result.output
        assert opened == ["http://1.2.3.4:9000/scope"]
        assert "access-gated" not in result.output


class TestStreamCapture:

    def test_the_script_gets_the_host_to_copy_from(self, monkeypatch):
        monkeypatch.setattr("cli.commands.box._ssh.resolve_box_user", lambda ip: "lagerdata")
        result, _, executed = _run(["pico1", "stream", "capture", "--box", "B"], nets=NETS)
        assert result.exit_code == 0, result.output
        assert _sent(executed)["params"]["scp_host"] == "lagerdata@1.2.3.4"

    def test_the_cli_claims_no_file_of_its_own(self, monkeypatch):
        """Only the script knows whether it wrote a file, and where."""
        monkeypatch.setattr("cli.commands.box._ssh.resolve_box_user", lambda ip: "lagerdata")
        result, _, _ = _run(["pico1", "stream", "capture", "--box", "B"], nets=NETS)
        assert result.exit_code == 0, result.output
        assert "saved on box" not in result.output
        assert "scp" not in result.output


class TestStreamConfig:

    def test_nothing_to_change_is_refused_before_the_box(self):
        result, _, executed = _run(["pico1", "stream", "config", "--box", "B"], nets=NETS)
        assert result.exit_code == 1
        assert "at least one setting" in result.output
        assert executed == []

    @pytest.mark.parametrize("net,channel", [("pico1", "A"), ("probe_b", "B")])
    def test_a_channel_setting_goes_to_the_net_own_channel(self, net, channel):
        result, _, executed = _run([net, "stream", "config", "-v", "0.5", "--box", "B"],
                                   nets=NETS)
        assert result.exit_code == 0, result.output
        assert _sent(executed)["params"] == {"netname": net, "channel": channel,
                                             "volts_per_div": 0.5}

    def test_a_named_channel_wins(self):
        result, _, executed = _run(
            ["probe_b", "stream", "config", "-c", "A", "--coupling", "ac", "--box", "B"],
            nets=NETS)
        assert result.exit_code == 0, result.output
        assert _sent(executed)["params"] == {"netname": "probe_b", "channel": "A",
                                             "coupling": "ac"}

    def test_a_scope_wide_setting_names_no_channel(self):
        result, _, executed = _run(
            ["pico1", "stream", "config", "--trigger-level", "0.2", "--box", "B"], nets=NETS)
        assert result.exit_code == 0, result.output
        assert _sent(executed)["params"] == {"netname": "pico1", "trigger_level": 0.2}
