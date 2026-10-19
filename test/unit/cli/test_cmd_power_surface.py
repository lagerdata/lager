# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager supply` and `lager battery`: the request each subcommand sends, and the
guards that stop one from being sent.

Only the TUIs had unit tests; the CLI commands themselves ran nowhere but the
bench. These are the commands that put voltage on a DUT, so the things pinned
here are the ones that cost hardware when they regress: an output change that
goes out without the confirmation it asks for, a protection limit that does
not reach the instrument, and a setpoint outside the limit it was given.
"""

import pytest


@pytest.fixture
def power(fake_box):
    fake_box.net("supply1", "power-supply", instrument="rigol_dp800")
    fake_box.net("battery1", "battery", instrument="keithley")
    return fake_box


def _supply(box):
    url, body = box.posts[-1]
    assert url == f"http://{box.IP}:9000/supply/command"
    return body


def _battery(box):
    url, body = box.posts[-1]
    assert url == f"http://{box.IP}:9000/battery/command"
    return body


# ---------------------------------------------------------------------------
# supply
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["voltage", "3.3"],
    ["current", "0.5"],
    ["enable"],
])
def test_supply_output_changes_are_not_sent_unconfirmed(lager, power, argv):
    # Declining is not an error (exit 0); what matters is that nothing went out.
    result = lager("supply", "supply1", *argv, input="n\n")
    assert "[y/N]" in result.output
    assert power.posts == []


@pytest.mark.parametrize("argv, action, params", [
    (["voltage", "3.3", "--yes"], "voltage",
     {"netname": "supply1", "value": 3.3, "ocp": None, "ovp": None}),
    (["current", "0.5", "--yes"], "current",
     {"netname": "supply1", "value": 0.5, "ocp": None, "ovp": None}),
    (["voltage", "3.3", "--ocp", "1", "--ovp", "4", "--yes"], "voltage",
     {"netname": "supply1", "value": 3.3, "ocp": 1.0, "ovp": 4.0}),
    (["enable", "--yes"], "enable", {"netname": "supply1"}),
    (["disable", "--yes"], "disable", {"netname": "supply1"}),
    (["clear-ocp"], "clear_ocp", {"netname": "supply1"}),
    (["clear-ovp"], "clear_ovp", {"netname": "supply1"}),
    (["state"], "state", {"netname": "supply1"}),
])
def test_supply_payload(lager, power, argv, action, params):
    result = lager("supply", "supply1", *argv)
    assert result.exit_code == 0, result.output
    assert _supply(power) == {"netname": "supply1", "action": action, "params": params}


def test_supply_voltage_without_a_value_is_a_read(lager, power):
    result = lager("supply", "supply1", "voltage")
    assert result.exit_code == 0, result.output
    assert _supply(power)["params"]["value"] is None


def test_supply_setpoint_above_its_own_ovp_is_refused(lager, power):
    result = lager("supply", "supply1", "voltage", "5", "--ovp", "4", "--yes")
    assert result.exit_code != 0
    assert "OVP limit (4.0V) cannot be less than voltage setpoint (5.0V)" in result.output
    assert power.posts == []


def test_supply_negative_setpoint_is_not_taken_as_a_value(lager, power):
    result = lager("supply", "supply1", "voltage", "-1", "--yes")
    assert result.exit_code == 2
    assert power.posts == []


def test_supply_on_a_battery_net_sends_nothing(lager, power):
    result = lager("supply", "battery1", "voltage", "3.3", "--yes")
    assert result.exit_code != 0
    assert power.posts == []


def test_supply_box_error_is_a_non_zero_exit(lager, power):
    power.status = 500
    power.answer = lambda url, body: {"success": False, "error": "instrument not connected"}
    result = lager("supply", "supply1", "state")
    assert result.exit_code != 0
    assert "instrument not connected" in result.output


# ---------------------------------------------------------------------------
# battery
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["enable"], ["disable"]])
def test_battery_output_changes_are_not_sent_unconfirmed(lager, power, argv):
    result = lager("battery", "battery1", *argv, input="n\n")
    assert "[y/N]" in result.output or "[Y/n]" in result.output
    assert power.posts == []


@pytest.mark.parametrize("argv, action, extra", [
    (["enable", "--yes"], "enable_battery", {}),
    (["disable", "--yes"], "disable_battery", {}),
    (["soc", "50"], "set_soc", {"value": 50.0}),
    (["voc", "4.2"], "set_voc", {"value": 4.2}),
    (["batt-full", "4.2"], "set_volt_full", {"value": 4.2}),
    (["batt-empty", "3.0"], "set_volt_empty", {"value": 3.0}),
    (["capacity", "2"], "set_capacity", {"value": 2.0}),
    (["current-limit", "1"], "set_current_limit", {"value": 1.0}),
    (["ocp", "2"], "set_ocp", {"value": 2.0}),
    (["ovp", "5"], "set_ovp", {"value": 5.0}),
    (["mode", "static"], "set_mode", {"mode_type": "static"}),
    (["model", "18650"], "set_model", {"partnumber": "18650"}),
    (["set"], "set_to_battery_mode", {}),
    (["clear"], "clear", {}),
    (["clear-ocp"], "clear_ocp", {}),
    (["clear-ovp"], "clear_ovp", {}),
    (["state"], "print_state", {}),
])
def test_battery_payload(lager, power, argv, action, extra):
    result = lager("battery", "battery1", *argv)
    assert result.exit_code == 0, result.output
    assert _battery(power) == {"netname": "battery1", "action": action,
                               "params": {"netname": "battery1", **extra}}


@pytest.mark.parametrize("sub", ["soc", "ocp", "ovp", "capacity", "batt-full"])
def test_battery_setter_without_a_value_is_a_read(lager, power, sub):
    result = lager("battery", "battery1", sub)
    assert result.exit_code == 0, result.output
    assert _battery(power)["params"]["value"] is None


@pytest.mark.parametrize("value", ["150", "100.1"])
def test_battery_soc_above_100_percent_is_refused(lager, power, value):
    result = lager("battery", "battery1", "soc", value)
    assert result.exit_code != 0
    assert "SOC must be between 0 and 100%" in result.output
    assert power.posts == []


def test_battery_box_error_is_a_non_zero_exit(lager, power):
    power.status = 500
    power.answer = lambda url, body: {"success": False, "error": "battery not responding"}
    result = lager("battery", "battery1", "state")
    assert result.exit_code != 0
