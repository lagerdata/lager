# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Splitting a scope net into the scope and its channels.

`role: "scope"` used to mean one channel of an oscilloscope, which left the
instrument with no representation -- so the timebase, the trigger and
run/stop, none of which belong to a channel, were addressed to whichever
channel net the caller happened to hold. It now means the instrument, and
`role: "scope-channel"` means a channel.

Reusing the string is only safe because every scope net saved before the
change is a channel: there was nothing else it could have been. That makes
the conversion total rather than a guess, and these tests hold it to that --
it has to convert every old record, invent exactly one net per physical unit,
and do nothing at all the second time it runs.
"""
from __future__ import annotations

import pytest

from lager.nets import scope_migration


def _channel(name, pin, instrument="picoscope_2000", address=""):
    rec = {"name": name, "role": "scope", "instrument": instrument, "pin": pin}
    if address:
        rec["address"] = address
    return rec


def _roles(nets):
    return {rec["name"]: rec["role"] for rec in nets}


class TestOldScopeNetsBecomeChannels:

    def test_every_one_of_them_converts(self):
        nets, changed = scope_migration.migrate(
            [_channel("scope1", 1), _channel("scope2", 2)])

        assert changed
        roles = _roles(nets)
        assert roles["scope1"] == "scope-channel"
        assert roles["scope2"] == "scope-channel"

    def test_the_channel_keeps_its_pin_and_name(self):
        """These name a signal on the board; renaming them would be theft."""
        nets, _ = scope_migration.migrate([_channel("vbus", 1)])

        channel = next(n for n in nets if n["name"] == "vbus")
        assert channel["pin"] == 1
        assert channel["instrument"] == "picoscope_2000"

    def test_nets_of_other_roles_are_untouched(self):
        original = [{"name": "gpi1", "role": "gpio", "instrument": "labjack_t7"},
                    _channel("scope1", 1)]
        nets, _ = scope_migration.migrate(original)

        assert _roles(nets)["gpi1"] == "gpio"


class TestTheUnitGetsANetOfItsOwn:

    def test_one_is_created(self):
        nets, _ = scope_migration.migrate(
            [_channel("scope1", 1), _channel("scope2", 2)])

        instruments = [n for n in nets if n["role"] == "scope"]
        assert len(instruments) == 1

    def test_it_is_named_after_the_model(self):
        """`picoscope1` says what it is; a stem shared by the channels would
        not, since those are named after what they probe."""
        nets, _ = scope_migration.migrate([_channel("vbus", 1)])

        instrument = next(n for n in nets if n["role"] == "scope")
        assert instrument["name"] == "picoscope1"

    def test_it_carries_no_pin_which_is_what_marks_it(self):
        nets, _ = scope_migration.migrate([_channel("scope1", 1)])

        instrument = next(n for n in nets if n["role"] == "scope")
        assert scope_migration.pin_of(instrument) is None

    def test_two_scopes_on_a_bench_get_one_each(self):
        nets, _ = scope_migration.migrate([
            _channel("a1", 1, address="usb::first"),
            _channel("a2", 2, address="usb::first"),
            _channel("b1", 1, instrument="Rigol_MSO5204", address="usb::second"),
        ])

        instruments = sorted(n["name"] for n in nets if n["role"] == "scope")
        assert instruments == ["picoscope1", "rigol1"]

    def test_the_channels_can_be_found_from_it(self):
        """Same instrument and address, which is how they are grouped."""
        nets, _ = scope_migration.migrate(
            [_channel("scope1", 1, address="usb::only")])

        instrument = next(n for n in nets if n["role"] == "scope")
        channel = next(n for n in nets if n["role"] == "scope-channel")
        assert scope_migration.unit_of(instrument) == scope_migration.unit_of(channel)

    def test_it_does_not_take_a_name_already_in_use(self):
        nets, _ = scope_migration.migrate(
            [_channel("scope1", 1),
             {"name": "picoscope1", "role": "gpio", "instrument": "labjack_t7"}])

        instrument = next(n for n in nets if n["role"] == "scope")
        assert instrument["name"] == "picoscope2"


class TestRunningItTwiceChangesNothing:

    def test_the_second_pass_reports_no_change(self):
        once, _ = scope_migration.migrate([_channel("scope1", 1)])
        twice, changed = scope_migration.migrate(once)

        assert changed is False
        assert twice == once

    def test_no_second_instrument_net_appears(self):
        nets, _ = scope_migration.migrate([_channel("scope1", 1)])
        for _ in range(3):
            nets, _ = scope_migration.migrate(nets)

        assert len([n for n in nets if n["role"] == "scope"]) == 1

    def test_an_already_migrated_file_is_left_alone(self):
        already = [
            {"name": "picoscope1", "role": "scope", "instrument": "picoscope_2000"},
            {"name": "vbus", "role": "scope-channel", "instrument": "picoscope_2000", "pin": 1},
        ]
        nets, changed = scope_migration.migrate(already)

        assert changed is False
        assert nets == already

    def test_a_box_with_no_scopes_is_left_alone(self):
        nets = [{"name": "gpi1", "role": "gpio", "instrument": "labjack_t7"}]
        assert scope_migration.migrate(nets) == (nets, False)


class TestThePinIsWhatTellsThemApart:

    def test_a_pin_in_the_mappings_counts(self):
        """Saved records carry the pin twice; either one identifies a channel."""
        rec = {"name": "scope1", "role": "scope", "instrument": "picoscope_2000",
               "mappings": [{"net": "scope1", "pin": 2, "location": "2"}]}
        assert scope_migration.pin_of(rec) == 2
        assert scope_migration.needs_migration([rec])

    def test_a_pinless_scope_net_is_taken_for_the_instrument(self):
        """Documented loss, and the better reading of the record.

        The pin was optional and the driver defaulted to channel 1, so such a
        net worked as channel A by accident. It names no channel, so it now
        describes the unit -- and a per-channel command sent to it says so
        rather than landing on the first channel unannounced.
        """
        rec = {"name": "scope1", "role": "scope", "instrument": "picoscope_2000"}

        assert scope_migration.needs_migration([rec]) is False
        assert scope_migration.migrate([rec]) == ([rec], False)


class TestTheRolesAreRoutable:
    """A new role that nothing dispatches is worse than no new role."""

    def test_both_reach_the_scope_handler(self):
        from lager.http_handlers import net_command

        assert net_command.ROLE_ACTIONS["scope"] is net_command._scope
        assert net_command.ROLE_ACTIONS["scope-channel"] is net_command._scope

    def test_both_build_the_same_driver(self):
        from lager.http_handlers import net_command

        assert net_command._HS_FACTORY["scope-channel"] == "scope_hs"
        assert net_command._HS_FACTORY["scope"] == "scope_hs"

    def test_the_new_role_has_a_net_type(self):
        from lager.nets.constants import NetType

        assert NetType.from_role("scope-channel") is NetType.from_role("scope")

    def test_a_scope_and_its_channels_share_one_lock(self):
        """They address one instrument, and a PicoScope's USB handle admits
        one owner, so they must queue rather than collide."""
        from lager.http_handlers.net_command import _physical_device_id as key

        rec = {"address": "", "unique_id": "PS2204A-11750"}
        assert (key("scope", "picoscope_2000", rec)
                == key("scope-channel", "picoscope_2000", rec))

    def test_a_rigol_is_no_longer_called_a_labjack(self):
        """It fell past every branch to the LabJack default.

        With an address that was merely wrong-looking, but an addressless
        Rigol collapsed onto "labjack:ANY" and shared a lock with a LabJack
        that had no address either.
        """
        from lager.http_handlers.net_command import _physical_device_id as key

        rigol = key("scope", "Rigol_MSO5204", {})
        labjack = key("gpio", "labjack_t7", {})
        assert not rigol.startswith("labjack:")
        assert rigol != labjack

    def test_a_picoscope_keeps_the_lock_key_it_had(self):
        """Changing it would split a lock that is protecting a USB handle."""
        from lager.http_handlers.net_command import _physical_device_id as key

        assert key("scope", "picoscope_2000", {"unique_id": "PS-1"}) == "picoscope:PS-1"


class TestASavedScopeNetSurvivesTheRoundTrip:
    """The migration must not eat a scope net that went through the saver.

    `Net.save_local_net` normalises every record, and with no pin to work
    from it writes zero into the mappings. Read back as channel zero, a scope
    net added through the TUI would have been converted into a channel on the
    very next read -- and the migration would have done the same to the net
    it had just invented itself.
    """

    def _saved_shape(self, name="picoscope1", pin=None):
        """What Net.save_local_net makes of a record: mappings, pin or zero."""
        pin_value = 0 if pin is None else pin
        rec = {"name": name, "role": "scope", "instrument": "picoscope_2000",
               "mappings": [{"net": name, "pin": pin_value,
                             "location": str(pin_value)}],
               "scope_points": [[pin_value, str(pin_value)]]}
        if pin is not None:
            rec["pin"] = pin
        return rec

    def test_the_filler_zero_is_not_read_as_a_channel(self):
        assert scope_migration.pin_of(self._saved_shape()) is None

    def test_such_a_net_is_left_as_the_instrument(self):
        rec = self._saved_shape()
        assert scope_migration.needs_migration([rec]) is False
        assert scope_migration.migrate([rec]) == ([rec], False)

    def test_a_real_channel_pin_still_reads(self):
        assert scope_migration.pin_of(self._saved_shape(pin=2)) == 2

    def test_a_string_pin_is_understood(self):
        """The CLI writes pins as strings."""
        rec = {"name": "scope1", "role": "scope",
               "instrument": "picoscope_2000", "pin": "1"}
        assert scope_migration.pin_of(rec) == "1"
        assert scope_migration.needs_migration([rec])

    def test_a_string_zero_is_still_the_filler(self):
        rec = {"name": "picoscope1", "role": "scope",
               "instrument": "picoscope_2000", "pin": "0"}
        assert scope_migration.pin_of(rec) is None
