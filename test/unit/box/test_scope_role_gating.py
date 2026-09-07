# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Which commands a scope net takes, and which its channels do.

A scope net is the instrument: the timebase, the horizontal position, the
trigger and run/stop belong to it. A scope-channel net is one channel:
coupling, volts/div, probe, enable and measurements belong to that.

The gate between them runs one way only, which reads like a half-finished
job unless the reason is written down. A channel net names exactly one
instrument, so a timebase sent to a channel is unambiguous -- it is carried
out, and every script written before scope nets existed keeps working. A
coupling sent to the instrument names no channel, and there is no honest
default: the driver falls back to channel A, so the command would land
somewhere plausible and wrong rather than failing.

So: device-wide commands are accepted anywhere on the scope, per-channel
commands are refused on the instrument, and the refusal names the channel
nets that would have worked.
"""
from __future__ import annotations

from unittest import mock

import pytest

from lager.http_handlers import net_command

NETS = [
    {"name": "pico1", "role": "scope", "instrument": "picoscope_2000",
     "address": "usb::one"},
    {"name": "vbus", "role": "scope-channel", "instrument": "picoscope_2000",
     "address": "usb::one", "pin": 1},
    {"name": "reset", "role": "scope-channel", "instrument": "picoscope_2000",
     "address": "usb::one", "pin": 2},
    # A second unit, whose channels must not be offered for the first.
    {"name": "rigol1", "role": "scope", "instrument": "Rigol_MSO5204",
     "address": "usb::two"},
    {"name": "clk", "role": "scope-channel", "instrument": "Rigol_MSO5204",
     "address": "usb::two", "pin": 1},
]

DEVICE_WIDE = [
    "start_capture", "start_single", "stop_capture", "force_trigger",
    "set_timebase", "get_timebase", "set_time_offset", "get_time_offset",
    "trigger_edge", "capabilities",
    "set_cursor", "get_cursor", "clear_cursor", "measure_cursor",
]

PER_CHANNEL = [
    "enable_net", "disable_net", "get_net_enabled",
    "set_scale", "get_scale", "set_coupling", "get_coupling",
    "set_probe", "get_probe", "set_offset", "get_offset",
    "measure_all", "measure_vpp", "measure_freq",
]


def _gate(netname, action):
    """Run only the role gate, without building a driver."""
    role = next(n["role"] for n in NETS if n["name"] == netname)
    with mock.patch.object(net_command, "Net") as NetMock:
        NetMock.get_local_nets.return_value = NETS
        net_command._refuse_channel_action_on_the_instrument(
            netname, role, action)


class TestTheInstrumentTakesWhatBelongsToIt:

    @pytest.mark.parametrize("action", DEVICE_WIDE)
    def test_device_wide_actions_pass(self, action):
        _gate("pico1", action)

    @pytest.mark.parametrize("action", PER_CHANNEL)
    def test_per_channel_actions_are_refused(self, action):
        with pytest.raises(net_command.UnknownAction):
            _gate("pico1", action)

    def test_the_refusal_names_the_channel_nets_that_would_work(self):
        with pytest.raises(net_command.UnknownAction) as caught:
            _gate("pico1", "set_coupling")

        message = str(caught.value)
        assert "vbus" in message and "reset" in message

    def test_it_does_not_offer_another_scope_channels(self):
        """A bench can hold two of the same model."""
        with pytest.raises(net_command.UnknownAction) as caught:
            _gate("rigol1", "set_coupling")

        message = str(caught.value)
        assert "clk" in message
        assert "vbus" not in message

    def test_an_instrument_with_no_channels_yet_says_how_to_add_one(self):
        with mock.patch.object(net_command, "Net") as NetMock:
            NetMock.get_local_nets.return_value = [NETS[0]]
            with pytest.raises(net_command.UnknownAction) as caught:
                net_command._refuse_channel_action_on_the_instrument(
                    "pico1", "scope", "set_coupling")

        assert "nets tui" in str(caught.value)


class TestAChannelTakesEverything:
    """Deliberate. A channel names one instrument, so nothing is ambiguous."""

    @pytest.mark.parametrize("action", PER_CHANNEL)
    def test_its_own_actions_pass(self, action):
        _gate("vbus", action)

    @pytest.mark.parametrize("action", DEVICE_WIDE)
    def test_device_wide_actions_pass_too(self, action):
        """`lager scope vbus timebase 1ms` predates the scope net and has to
        keep working; the channel resolves to one instrument."""
        _gate("vbus", action)


class TestTheTwoListsAgreeWithTheHandler:
    """A new action defaulting to device-wide is the safe direction, but a
    per-channel one left off the list would land on channel A in silence."""

    def test_every_measurement_is_treated_as_per_channel(self):
        for action in net_command._SCOPE_MEASUREMENTS:
            with pytest.raises(net_command.UnknownAction):
                _gate("pico1", action)

    def test_the_per_channel_list_holds_no_unknown_actions(self):
        """Guards against a rename leaving a dead entry behind, which would
        quietly stop gating the action it used to name."""
        import inspect

        source = inspect.getsource(net_command._scope)
        for action in net_command._PER_CHANNEL_SCOPE_ACTIONS:
            assert '"%s"' % action in source, action

    def test_nothing_is_in_both_lists(self):
        assert not (net_command._PER_CHANNEL_SCOPE_ACTIONS
                    & set(net_command._SCOPE_MEASUREMENTS)) - {"measure_all"}


class TestTheRefusalReadsAsASentence:
    """It is the only message a user sees when they address the wrong net,
    so it has to say what to do rather than merely that something is wrong.

    The route wraps UnknownAction in "Unknown action '%s' for %s", which
    suits an action name and not a sentence: reusing it turned the guidance
    into `Unknown action 'pico1 is the scope itself, and set_coupling acts on
    one channel -- use one of its channel nets: scope1, scope2' for scope`.
    """

    def _error(self, netname="pico1", action="set_coupling"):
        from unittest.mock import patch

        from lager.http_handlers import net_command as nc

        app = __import__("flask").Flask(__name__)
        nc.register_net_command_routes(app)
        with patch.object(nc, "Net") as NetMock:
            NetMock.get_local_nets.return_value = NETS
            client = app.test_client()
            r = client.post("/net/command", json={
                "netname": netname, "action": action,
                "params": {"coupling": "ac"}})
        return r.status_code, (r.get_json() or {}).get("error", "")

    def test_it_is_still_a_400(self):
        assert self._error()[0] == 400

    def test_it_is_not_wrapped_as_an_unknown_action(self):
        assert "Unknown action" not in self._error()[1]

    def test_it_names_the_nets_that_would_work(self):
        message = self._error()[1]
        assert "vbus" in message and "reset" in message

    def test_a_genuinely_unknown_action_still_says_so(self):
        """The wrapping is right for what it was written for."""
        status, message = self._error(netname="vbus", action="set_nonsense")
        assert status == 400
        assert "Unknown action" in message

    def test_the_refusal_is_catchable_as_an_unknown_action(self):
        """Existing handlers catch the base class."""
        from lager.http_handlers.net_command import (
            UnknownAction, WrongNetForAction)

        assert issubclass(WrongNetForAction, UnknownAction)
