#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Two devices of one model on one box.

``nets add``, ``nets add-all``, the net TUI and ``lager instruments`` used to
each carry their own copy of a hardcoded ``_MULTI_HUBS`` model set, and each did
something different with it: ``add`` hard-errored, ``add-all`` silently skipped
the whole family, the TUI computed per-device keys and then discarded them, and
``instruments`` hid the devices from its own table so you could not read the
addresses needed to create their nets.

That is the wrong question. Whether a second device is usable is a property of
the ADDRESS, not of the model:

* an Acroname 8-port reports a unique serial, so two of them get two addresses
  and both stay drivable;
* a Plugable hub reports no serial but IS topology-addressed by the scanner
  (``_TOPOLOGY_ADDRESSED``) precisely so duplicates stay distinguishable;
* a LabJack T7 reports no serial and is not topology-addressed, so it
  enumerates as ``USB0::0x0CD5::0x0007::::INSTR`` on every box measured. Two of
  those are byte-identical and genuinely cannot be told apart.

The stakes are not abstract. ``delete-all`` + ``add-all`` is the documented
bench-recovery path, and the CI bench's AC power relays are LabJack GPIO nets.
A guard that skips the LabJack family silently takes the bench's instrument
power with it, which is what test 8 pins.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from test.unit.cli.nets_http_fake import FakeBoxHTTP  # noqa: E402

nets_mod = importlib.import_module('cli.commands.box.nets')
from cli.commands.box.nets import nets as nets_group  # noqa: E402
from cli.commands.box._device_identity import ambiguous_addresses  # noqa: E402


# Addresses exactly as the scanner emits them. The LabJack's empty serial slot
# is not a typo -- it is what four separate box dumps and the live CI bench all
# report, and it is the whole reason this guard has to exist at all.
LABJACK_ADDR = "USB0::0x0CD5::0x0007::::INSTR"
ACRO_A_ADDR = "USB0::0x24FF::0x0013::EBFB8D94::INSTR"
ACRO_B_ADDR = "USB0::0x24FF::0x0013::56BCCDD3::INSTR"


def _labjack(addr=LABJACK_ADDR):
    return {
        "name": "LabJack_T7", "vid": "0cd5", "pid": "0007", "serial": "",
        "address": addr,
        "net_type": ["gpio", "adc", "dac"],
        "channels": {
            "gpio": ["FIO0", "FIO1", "FIO2"],
            "adc": ["AIN0", "AIN1"],
            "dac": ["DAC0", "DAC1"],
        },
    }


def _acroname(addr, serial):
    return {
        "name": "Acroname_8Port", "vid": "24ff", "pid": "0013", "serial": serial,
        "address": addr,
        "net_type": ["usb"],
        "channels": {"usb": ["0", "1", "2", "3", "4", "5", "6", "7"]},
    }


ACRO_A = _acroname(ACRO_A_ADDR, "EBFB8D94")
ACRO_B = _acroname(ACRO_B_ADDR, "56BCCDD3")


@pytest.fixture
def box_factory():
    """Build a fake box from an arbitrary instrument list.

    A factory rather than a fixture per shape, because every test here varies
    the instrument list -- that IS the variable under test.
    """
    started: list = []

    def _make(instruments):
        box = FakeBoxHTTP(instruments)
        for patcher in (
            patch("requests.request", box.request),
            patch("cli.box_storage.resolve_and_validate_box",
                  lambda _ctx, name: name or "testbox"),
            patch.object(nets_mod, "_resolve_box",
                         lambda _ctx, name: name or "testbox"),
        ):
            patcher.start()
            started.append(patcher)
        return box

    yield _make

    for patcher in reversed(started):
        patcher.stop()


def _invoke(args, input=None):
    result = CliRunner().invoke(nets_group, args, input=input, catch_exceptions=False)
    try:
        stderr = result.stderr
    except ValueError:
        stderr = ""
    if stderr and stderr not in result.output:
        result.output_bytes = (result.output + stderr).encode()
    return result


# --------------------------------------------------------------------------- #
# the helper itself                                                           #
# --------------------------------------------------------------------------- #

class TestAmbiguousAddresses:
    def test_distinct_serials_are_not_ambiguous(self):
        assert ambiguous_addresses([ACRO_A, ACRO_B]) == set()

    def test_identical_addresses_are_ambiguous(self):
        assert ambiguous_addresses([_labjack(), _labjack()]) == {LABJACK_ADDR}

    def test_single_device_is_never_ambiguous(self):
        assert ambiguous_addresses([_labjack()]) == set()

    def test_mixed_bench_isolates_only_the_clashing_model(self):
        devs = [ACRO_A, ACRO_B, _labjack(), _labjack()]
        assert ambiguous_addresses(devs) == {LABJACK_ADDR}

    def test_missing_addresses_are_ignored_not_collapsed(self):
        # Two devices with no address must not look like one shared address.
        assert ambiguous_addresses([{"name": "x"}, {"name": "y"}]) == set()


# --------------------------------------------------------------------------- #
# 1 + 2: nets add                                                              #
# --------------------------------------------------------------------------- #

class TestNetsAdd:
    def test_add_on_one_of_two_distinct_devices_succeeds(self, box_factory):
        """A second Acroname must not block the first. Regression: the old
        guard counted devices by MODEL NAME and refused, even though the
        address the user typed already named exactly one of them."""
        box = box_factory([ACRO_A, ACRO_B])
        result = _invoke(["add", "usb1", "usb", "0", ACRO_A_ADDR, "--box", "b"])
        assert result.exit_code == 0, result.output
        assert len(box.saved_nets) == 1
        assert box.saved_nets[0]["address"] == ACRO_A_ADDR

    def test_add_on_the_second_device_also_succeeds(self, box_factory):
        box = box_factory([ACRO_A, ACRO_B])
        result = _invoke(["add", "usb9", "usb", "0", ACRO_B_ADDR, "--box", "b"])
        assert result.exit_code == 0, result.output
        assert box.saved_nets[0]["address"] == ACRO_B_ADDR

    def test_add_on_a_genuinely_ambiguous_address_is_refused(self, box_factory):
        box = box_factory([_labjack(), _labjack()])
        result = _invoke(["add", "gpio1", "gpio", "FIO0", LABJACK_ADDR, "--box", "b"])
        assert result.exit_code != 0
        assert "more than one device reports this address" in result.output
        assert box.saved_nets == []

    def test_refusal_explains_the_cause_not_just_a_remedy(self, box_factory):
        box_factory([_labjack(), _labjack()])
        result = _invoke(["add", "gpio1", "gpio", "FIO0", LABJACK_ADDR, "--box", "b"])
        assert "no unique serial number" in result.output

    def test_single_labjack_still_adds(self, box_factory):
        """The guard must cost nothing on a normal one-device bench."""
        box = box_factory([_labjack()])
        result = _invoke(["add", "gpio1", "gpio", "FIO0", LABJACK_ADDR, "--box", "b"])
        assert result.exit_code == 0, result.output
        assert len(box.saved_nets) == 1


# --------------------------------------------------------------------------- #
# 3 + 5 + 6: nets add-all                                                      #
# --------------------------------------------------------------------------- #

class TestNetsAddAll:
    def test_two_acronames_yield_all_sixteen_usb_nets(self, box_factory):
        """Regression, two bugs at once: the family was silently skipped, and
        ``chan_seen`` was keyed by model so device B's channel "0" collided
        with device A's. Naming already handled this -- indices are assigned
        per role across all devices."""
        box = box_factory([ACRO_A, ACRO_B])
        result = _invoke(["add-all", "--box", "b"], input="y\n")
        assert result.exit_code == 0, result.output
        assert len(box.saved_nets) == 16, [n["name"] for n in box.saved_nets]
        names = {n["name"] for n in box.saved_nets}
        assert names == {f"usb{i}" for i in range(1, 17)}
        # each physical device contributed all eight of its ports
        per_addr = {}
        for n in box.saved_nets:
            per_addr.setdefault(n["address"], set()).add(str(n["pin"]))
        assert per_addr[ACRO_A_ADDR] == {str(i) for i in range(8)}
        assert per_addr[ACRO_B_ADDR] == {str(i) for i in range(8)}

    def test_two_labjacks_are_refused_and_say_so(self, box_factory):
        """The old code skipped the family in SILENCE, which on the CI bench
        means losing the AC relay nets with no message."""
        box = box_factory([_labjack(), _labjack()])
        result = _invoke(["add-all", "--box", "b"], input="y\n")
        assert not any(n["instrument"] == "LabJack_T7" for n in box.saved_nets)
        assert "more than one device reports this address" in result.output

    def test_ambiguous_model_does_not_block_the_addressable_ones(self, box_factory):
        """Two LabJacks must not take the Acronames down with them."""
        box = box_factory([ACRO_A, ACRO_B, _labjack(), _labjack()])
        result = _invoke(["add-all", "--box", "b"], input="y\n")
        assert result.exit_code == 0, result.output
        usb = [n for n in box.saved_nets if n["role"] == "usb"]
        assert len(usb) == 16
        assert not any(n["instrument"] == "LabJack_T7" for n in box.saved_nets)

    def test_one_labjack_plus_one_acroname_is_untouched(self, box_factory):
        """One hub plus one DAQ -- the common bench. Nothing is ambiguous,
        so nothing is filtered."""
        box = box_factory([ACRO_A, _labjack()])
        result = _invoke(["add-all", "--box", "b"], input="y\n")
        assert result.exit_code == 0, result.output
        insts = {n["instrument"] for n in box.saved_nets}
        assert insts == {"Acroname_8Port", "LabJack_T7"}


# --------------------------------------------------------------------------- #
# 8: the bench-safety regression                                               #
# --------------------------------------------------------------------------- #

class TestSingleDeviceBenchUnchanged:
    """A bench where every instrument is singular -- the shape of the hardware
    CI runner. The guard must be a complete no-op here: the same nets before
    and after this change. This is the test that would catch a fix that
    accidentally took the bench's AC relay nets with it."""

    BENCH = [
        _acroname("USB0::0x24FF::0x0013::30DAABB5::INSTR", "30DAABB5"),
        _labjack(),
        {"name": "Rigol_DP821", "vid": "1ab1", "pid": "0e11", "serial": "DP8G232400080",
         "address": "USB0::0x1AB1::0x0E11::DP8G232400080::INSTR",
         "net_type": ["power-supply"], "channels": {"power-supply": ["1", "2"]}},
        {"name": "Keithley_2281S", "vid": "05e6", "pid": "2281", "serial": "4518305",
         "address": "USB0::0x05E6::0x2281::4518305::INSTR",
         "net_type": ["battery", "power-supply"],
         "channels": {"battery": ["1"], "power-supply": ["1"]}},
    ]

    def test_nothing_is_ambiguous(self):
        assert ambiguous_addresses(self.BENCH) == set()

    def test_add_all_creates_every_net(self, box_factory):
        box = box_factory(list(self.BENCH))
        result = _invoke(["add-all", "--box", "b"], input="y\n")
        assert result.exit_code == 0, result.output
        insts = {n["instrument"] for n in box.saved_nets}
        assert "LabJack_T7" in insts, "the bench's AC relay nets live here"
        assert "Acroname_8Port" in insts
        gpio = [n for n in box.saved_nets if n["role"] == "gpio"]
        assert gpio, "no GPIO nets means no RIGOL_POWER/KEITHLEY_POWER"

    def test_no_warnings_on_a_clean_bench(self, box_factory):
        box_factory(list(self.BENCH))
        result = _invoke(["add-all", "--box", "b"], input="y\n")
        assert "more than one device reports" not in result.output
