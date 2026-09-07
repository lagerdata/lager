# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Trigger coupling and channel coupling are different settings sharing a word.

Channel coupling is the input path: AC drops the DC component of the signal,
which moves the trace on screen. Trigger coupling is a filter on the way to
the comparator -- DC, AC, LF-reject, HF-reject -- so a drifting or noisy edge
can be triggered on without changing what is displayed.

The box's warm handler conflated them: `trigger coupling ac` called
`set_channel_coupling`, so asking for a trigger filter switched the input
instead and the waveform jumped. On a PicoScope there is no trigger filter at
all, and the old code silently applied one to the input.

Two nearby traps these tests also cover. `--coupling` accepts `low_freq_rej`
and `high_freq_rej`, which are trigger-path values and were never valid
channel coupling, so the conflation could not even round-trip. And `--mode`
and `--coupling` used to arrive defaulted, which made every trigger command
re-apply both -- adjusting a level put a single-armed trigger back to normal.
"""
from __future__ import annotations

import pytest

from lager.measurement.scope.picoscope import PicoScope, UnsupportedScopeFeature


def _invoke(action, params, device):
    from lager.http_handlers import net_command

    original = net_command._proxy
    net_command._proxy = lambda *a, **k: device
    try:
        return net_command._scope("scope1", "scope", action, params)
    finally:
        net_command._proxy = original


class _RecordingScope:
    """Records which setter each trigger field reached."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **_kwargs):
            self.calls.append((name, args))
            return {"status": "ok"}
        return call


class TestATriggerFilterDoesNotTouchTheInput:

    def test_trigger_coupling_sets_the_trigger_not_the_channel(self):
        device = _RecordingScope()
        _invoke("trigger_edge", {"coupling": "ac"}, device)

        setters = [name for name, _ in device.calls]
        assert "set_trigger_coupling" in setters
        assert "set_channel_coupling" not in setters, (
            "asking for a trigger filter switched the channel's input "
            "coupling, which moves the trace on screen")

    def test_the_value_is_passed_through_unchanged(self):
        """LF/HF-reject are trigger-only, so they must not be remapped."""
        for value in ("dc", "ac", "low_freq_rej", "high_freq_rej"):
            device = _RecordingScope()
            _invoke("trigger_edge", {"coupling": value}, device)
            assert ("set_trigger_coupling", (value,)) in device.calls

    def test_the_other_trigger_fields_still_reach_their_own_setters(self):
        device = _RecordingScope()
        _invoke("trigger_edge",
                {"source": "A", "slope": "rising", "level": 1.2,
                 "mode": "single"}, device)

        setters = [name for name, _ in device.calls]
        for expected in ("set_trigger_source", "set_trigger_slope",
                         "set_trigger_level", "set_capture_mode"):
            assert expected in setters

    def test_only_the_fields_given_are_applied(self):
        """A level tweak must not re-apply a coupling or a mode."""
        device = _RecordingScope()
        _invoke("trigger_edge", {"level": 0.5}, device)

        assert [name for name, _ in device.calls] == ["set_trigger_level"]


class TestAPicoScopeSaysItHasNoTriggerFilter:

    def test_setting_it_raises_rather_than_doing_nothing(self):
        """Silently ignoring leaves a scope that will not trigger.

        The setting would report as applied while the edge kept being missed,
        which is a worse outcome than being told the filter does not exist.
        """
        scope = PicoScope(netname="scope1", pin=1)
        with pytest.raises(UnsupportedScopeFeature) as caught:
            scope.set_trigger_coupling("ac")

        message = str(caught.value)
        # Names the setting that probably was meant, since the two share a
        # word and the mistake is easy to make.
        assert "trigger coupling" in message
        assert "coupling ac" in message

    def test_reading_it_raises_too(self):
        scope = PicoScope(netname="scope1", pin=1)
        with pytest.raises(UnsupportedScopeFeature):
            scope.get_trigger_coupling()

    def test_the_channel_coupling_setter_is_still_there(self):
        """The suggestion in the message has to be real."""
        scope = PicoScope(netname="scope1", pin=1)
        assert callable(scope.set_channel_coupling)


class TestTheCliDoesNotForceSettingsNobodyAskedFor:

    def _edge_params(self, argv):
        """The params `lager scope <net> trigger edge ...` would send."""
        import importlib
        import json
        import types
        from unittest import mock

        from click.testing import CliRunner

        scope_cli = importlib.import_module(
            "cli.commands.measurement.scope")

        sent = {}

        def capture(ctx, path, box_ip, env=(), **_kwargs):
            for entry in env:
                if entry.startswith("LAGER_COMMAND_DATA="):
                    sent.update(json.loads(entry.split("=", 1)[1]))

        with mock.patch.object(scope_cli, "run_python_internal", capture), \
                mock.patch.object(scope_cli, "_resolve_box",
                                  lambda *a, **k: "1.2.3.4"), \
                mock.patch.object(scope_cli, "_validate_scope_net",
                                  lambda *a, **k: {"name": "scope1"}), \
                mock.patch.object(scope_cli, "_require_netname",
                                  lambda *a, **k: "scope1"):
            result = CliRunner().invoke(
                scope_cli.scope, ["scope1", "trigger", "edge"] + argv,
                obj=types.SimpleNamespace(netname="scope1"))
        assert result.exit_code == 0, result.output
        return sent.get("params", {})

    def test_a_level_change_sends_no_coupling_and_no_mode(self):
        """The bug: both defaulted, so every call re-applied them."""
        params = self._edge_params(["--level", "1.2"])

        assert params.get("level") == 1.2
        assert params.get("coupling") is None, (
            "a level change also forced the trigger coupling to DC")
        assert params.get("mode") is None, (
            "a level change also put the trigger mode back to normal")

    def test_what_is_asked_for_is_still_sent(self):
        params = self._edge_params(
            ["--level", "0.5", "--coupling", "high_freq_rej",
             "--mode", "single", "--slope", "falling"])

        assert params["coupling"] == "high_freq_rej"
        assert params["mode"] == "single"
        assert params["slope"] == "falling"

    def test_the_help_distinguishes_the_two_couplings(self):
        """They share a word, so the flag has to say which one it is."""
        import importlib
        import types

        from click.testing import CliRunner

        scope_cli = importlib.import_module(
            "cli.commands.measurement.scope")

        output = CliRunner().invoke(
            scope_cli.scope, ["scope1", "trigger", "edge", "--help"],
            obj=types.SimpleNamespace(netname=None)).output
        assert "trigger-path" in output.lower() or "trigger path" in output.lower()
